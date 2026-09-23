package hooks

import (
	"context"
	"encoding/json"
	iofs "io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/bootstrap/packs/core"
)

// mimoCodePluginPackPath is the embedded MiMo Code plugin the hooks installer
// materializes into {workDir}/.mimocode/plugin/gascity.js.
const mimoCodePluginPackPath = "overlay/per-provider/mimocode/.mimocode/plugin/gascity.js"

// fakeMimoCodeGCDriver is a tiny executable `gc` stand-in. `prime --hook`
// returns a constant session context; `nudge drain --inject` returns a new
// current-time line on every call, exactly like the real consumptive hook;
// `mail check --inject` returns nothing. It is CommonJS (.cjs) so the staged
// plugin's package.json module type cannot reinterpret it.
//
// Every invocation first reads hook stdin to EOF, mirroring the real gc hooks:
// `gc nudge drain --inject` blocks in io.ReadAll until stdin closes, and
// `gc prime --hook` burns its bounded read timeout otherwise. The plugin must
// close execFile's stdin pipe or this read never returns and each call is
// killed at the plugin's 30 s timeout.
const fakeMimoCodeGCDriver = `#!/usr/bin/env node
const fs = require("node:fs");
const command = process.argv.slice(2).join(" ");
fs.readFileSync(0, "utf8");
if (command === "prime --hook") {
  process.stdout.write("PRIME-CONTEXT-STABLE\n");
} else if (command === "nudge drain --inject") {
  const counterFile = process.env.GC_FAKE_COUNTER;
  let n = 0;
  try {
    n = Number(fs.readFileSync(counterFile, "utf8")) || 0;
  } catch {}
  n += 1;
  fs.writeFileSync(counterFile, String(n));
  process.stdout.write(
    "Current time: 2026-09-23T05:15:" + String(n).padStart(2, "0") + "Z UTC (epoch " + (1000 + n) + ")\n",
  );
} else if (command === "mail check --inject") {
  process.stdout.write("");
}
`

// mimoCodePromptDriver negotiates two consecutive turns against one plugin
// instance and prints the resulting system prompt, per-message system override,
// and user-message text parts as JSON.
const mimoCodePromptDriver = `import { pathToFileURL } from "node:url";

const [pluginPath, directory, gcBin] = process.argv.slice(2);
process.env.GC_BIN = gcBin;
const { default: gascityPlugin } = await import(pathToFileURL(pluginPath).href);
const hooks = await gascityPlugin({ directory, client: {} });

async function turn(index) {
  const systemOut = { system: ["ROLE PROMPT"] };
  await hooks["experimental.chat.system.transform"]({ sessionID: "ses_test" }, systemOut);

  const messageID = ` + "`msg_${index}`" + `;
  const messageOut = {
    message: {
      id: messageID,
      sessionID: "ses_test",
      role: "user",
      agent: "build",
      model: { providerID: "p", modelID: "m" },
      time: { created: Date.now() },
    },
    parts: [
      {
        id: ` + "`part_${index}`" + `,
        sessionID: "ses_test",
        messageID,
        type: "text",
        text: ` + "`user turn ${index}`" + `,
      },
    ],
  };
  await hooks["chat.message"](
    { sessionID: "ses_test", agent: "build", model: { providerID: "p", modelID: "m" }, messageID },
    messageOut,
  );

  return {
    system: systemOut.system,
    messageSystem: messageOut.message.system,
    parts: messageOut.parts.map((part) => part.text),
  };
}

const first = await turn(1);
const second = await turn(2);
console.log(JSON.stringify({ first, second }));
`

type mimoCodeTurn struct {
	System        []string `json:"system"`
	MessageSystem string   `json:"messageSystem"`
	Parts         []string `json:"parts"`
}

const (
	// mimoCodePluginRunBudget hard-caps the node driver so a plugin that leaves
	// the child gc's stdin pipe open fails instead of stalling the suite through
	// the plugin's per-call 30 s timeouts.
	mimoCodePluginRunBudget = 20 * time.Second
	// mimoCodePluginPromptBudget is the prompt-completion assertion. Real gc
	// calls return in milliseconds; the plugin's own per-call timeout is 30 s,
	// so completing well under that proves the stdin pipe was closed rather than
	// timing out.
	mimoCodePluginPromptBudget = 10 * time.Second
)

// TestMimoCodePluginKeepsSystemPromptByteStableAcrossTurns executes the
// embedded plugin under node with a fake gc whose current-time line changes on
// every call and which reads hook stdin to EOF. Two consecutive turns must
// build a byte-identical system prompt (and byte-identical per-message system
// override), while the volatile clock must land in the newest user message's
// parts. A per-turn clock in the system prompt is what caps the DeepSeek prefix
// cache at the role prompt. Because the fake gc consumes stdin, the test also
// proves the plugin closes execFile's stdin pipe: a plugin that does not leaves
// every `gc nudge drain --inject` (and `gc prime --hook`) blocked until the 30 s
// timeout, so the clock/nudge injection never reaches the user-message tail.
func TestMimoCodePluginKeepsSystemPromptByteStableAcrossTurns(t *testing.T) {
	nodeBin, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node not installed; cannot execute the MiMo Code plugin")
	}

	plugin, err := iofs.ReadFile(core.PackFS, mimoCodePluginPackPath)
	if err != nil {
		t.Fatalf("read embedded MiMo Code plugin: %v", err)
	}

	stage := t.TempDir()
	if err := os.WriteFile(filepath.Join(stage, "gascity.js"), plugin, 0o644); err != nil {
		t.Fatalf("stage plugin: %v", err)
	}
	// MiMo Code loads workdir plugins as ES modules.
	if err := os.WriteFile(filepath.Join(stage, "package.json"), []byte(`{"type":"module"}`), 0o644); err != nil {
		t.Fatalf("stage package.json: %v", err)
	}
	driverPath := filepath.Join(stage, "driver.mjs")
	if err := os.WriteFile(driverPath, []byte(mimoCodePromptDriver), 0o644); err != nil {
		t.Fatalf("stage driver: %v", err)
	}

	gcDir := t.TempDir()
	gcPath := filepath.Join(gcDir, "fake-gc.cjs")
	if err := os.WriteFile(gcPath, []byte(fakeMimoCodeGCDriver), 0o755); err != nil {
		t.Fatalf("stage fake gc: %v", err)
	}
	counterFile := filepath.Join(gcDir, "counter")

	// The fake gc blocks on stdin until EOF, so the harness budget fails the
	// test promptly if the plugin ever stops closing the child's stdin pipe.
	ctx, cancel := context.WithTimeout(context.Background(), mimoCodePluginRunBudget)
	defer cancel()

	cmd := exec.CommandContext(ctx, nodeBin, driverPath, filepath.Join(stage, "gascity.js"), stage, gcPath)
	cmd.WaitDelay = 5 * time.Second
	cmd.Env = []string{
		"HOME=" + stage,
		"PATH=" + os.Getenv("PATH"),
		"GC_FAKE_COUNTER=" + counterFile,
	}
	start := time.Now()
	out, err := cmd.CombinedOutput()
	elapsed := time.Since(start)
	if err != nil {
		if ctx.Err() != nil {
			t.Fatalf("node plugin driver did not finish within %s; the plugin must close the child gc's stdin pipe (pending.child.stdin?.end()): %v\noutput:\n%s", mimoCodePluginRunBudget, err, out)
		}
		t.Fatalf("node plugin driver: %v\noutput:\n%s", err, out)
	}
	if elapsed >= mimoCodePluginPromptBudget {
		t.Fatalf("plugin gc calls took %s, want under %s: the fake gc reads stdin to EOF, so prompt completion proves the plugin closed execFile's stdin pipe instead of blocking until the 30 s timeout", elapsed, mimoCodePluginPromptBudget)
	}

	var got struct {
		First  mimoCodeTurn `json:"first"`
		Second mimoCodeTurn `json:"second"`
	}
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("parse driver output: %v\noutput:\n%s", err, out)
	}

	if len(got.First.System) == 0 || len(got.Second.System) == 0 {
		t.Fatalf("driver returned empty system prompts: first=%v second=%v", got.First.System, got.Second.System)
	}
	if !reflect.DeepEqual(got.First.System, got.Second.System) {
		t.Fatalf("system prompt changed between turns:\nfirst:  %q\nsecond: %q", got.First.System, got.Second.System)
	}
	if !strings.Contains(got.First.System[0], "ROLE PROMPT") ||
		!strings.Contains(got.First.System[0], "PRIME-CONTEXT-STABLE") {
		t.Fatalf("system prompt did not carry the stable role/prime context: %q", got.First.System[0])
	}
	if strings.Contains(got.First.System[0], "Current time:") {
		t.Fatalf("system prompt still contains per-turn clock bytes: %q", got.First.System[0])
	}

	if got.First.MessageSystem != got.Second.MessageSystem {
		t.Fatalf("per-message system override changed between turns:\nfirst:  %q\nsecond: %q", got.First.MessageSystem, got.Second.MessageSystem)
	}
	if !strings.Contains(got.First.MessageSystem, "PRIME-CONTEXT-STABLE") ||
		strings.Contains(got.First.MessageSystem, "Current time:") {
		t.Fatalf("per-message system override is not the stable prime context: %q", got.First.MessageSystem)
	}

	for i, turn := range []mimoCodeTurn{got.First, got.Second} {
		if len(turn.Parts) != 2 {
			t.Fatalf("turn %d user parts = %d, want original text plus one volatile injection: %q", i+1, len(turn.Parts), turn.Parts)
		}
		if !strings.HasPrefix(turn.Parts[0], "user turn ") {
			t.Fatalf("turn %d first part = %q, want the original user text first", i+1, turn.Parts[0])
		}
		if !strings.Contains(turn.Parts[1], "Current time:") {
			t.Fatalf("turn %d tail part = %q, want the volatile clock injection", i+1, turn.Parts[1])
		}
	}
	if got.First.Parts[1] == got.Second.Parts[1] {
		t.Fatalf("volatile injection did not change between turns: %q", got.First.Parts[1])
	}
	if !strings.Contains(got.Second.Parts[1], "epoch 1002") {
		t.Fatalf("second turn did not receive the second clock reading: %q", got.Second.Parts[1])
	}
}
