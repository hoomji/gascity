package hooks

import (
	"encoding/json"
	iofs "io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

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
const fakeMimoCodeGCDriver = `#!/usr/bin/env node
const fs = require("node:fs");
const command = process.argv.slice(2).join(" ");
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

// TestMimoCodePluginKeepsSystemPromptByteStableAcrossTurns executes the
// embedded plugin under node with a fake gc whose current-time line changes on
// every call. Two consecutive turns must build a byte-identical system prompt
// (and byte-identical per-message system override), while the volatile clock
// must land in the newest user message's parts. A per-turn clock in the system
// prompt is what caps the DeepSeek prefix cache at the role prompt.
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

	cmd := exec.Command(nodeBin, driverPath, filepath.Join(stage, "gascity.js"), stage, gcPath)
	cmd.Env = []string{
		"HOME=" + stage,
		"PATH=" + os.Getenv("PATH"),
		"GC_FAKE_COUNTER=" + counterFile,
	}
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("node plugin driver: %v\noutput:\n%s", err, out)
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
