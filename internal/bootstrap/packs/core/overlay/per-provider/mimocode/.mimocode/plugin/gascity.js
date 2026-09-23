// Gas City hooks for MiMo Code.
// Installed by gc into {workDir}/.mimocode/plugin/gascity.js
//
// MiMo Code is an OpenCode fork and exposes the same ESM, hook-oriented
// plugin API:
//   - event() is side-effect-only (no prompt injection)
//   - experimental.chat.system.transform mutates output.system
//   - chat.message mutates the newest user message and its parts
//   - experimental.session.compacting → inject context before compaction
//
// Gas City uses:
//   - session.created / session.compacted → gc prime --hook (side effects such
//     as session-id persistence and poller bootstrap)
//   - experimental.session.compacting → gc handoff --auto "context cycle"
//     and inject the handoff confirmation into the compaction context
//   - experimental.chat.system.transform → inject only the session-cached
//     gc prime --hook context into the system prompt. These bytes must stay
//     identical from turn to turn: the provider prefix cache keys on the
//     request head, so one per-turn line here evicts the whole cached
//     conversation prefix.
//   - chat.message → append the per-turn volatile injections (the current-time
//     line and queued nudges from gc nudge drain --inject, plus unread mail
//     from gc mail check --inject) to the tail of the newest user message.
//     History keeps its bytes, so only the new tail is a cache miss.

import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const GC_MIMOCODE_HOOK_VERSION = 3;
const GC_BIN = process.env.GC_BIN || "gc";
// GC_BIN is the explicit override. The fallback order matches Pi hooks so
// sibling providers resolve the same installed gc before developer-local bins.
const PATH_PREFIX =
  `/opt/homebrew/bin:/usr/local/bin:${process.env.HOME}/go/bin:${process.env.HOME}/.local/bin:`;

async function runCommand(directory, args, warnOnFailure, extraEnv = {}) {
  try {
    const { stdout, stderr } = await execFileAsync(GC_BIN, args, {
      cwd: directory,
      encoding: "utf-8",
      timeout: 30000,
      env: {
        ...process.env,
        ...extraEnv,
        PATH: PATH_PREFIX + (process.env.PATH || ""),
      },
    });
    logRunStderr(stderr);
    return stdout.trim();
  } catch (err) {
    if (warnOnFailure) {
      logRunFailure(args, directory, err);
    }
    return "";
  }
}

async function run(directory, ...args) {
  return runCommand(directory, args, false);
}

async function runWithWarning(directory, ...args) {
  return runCommand(directory, args, true);
}

function logRunFailure(args, directory, err) {
  try {
    const detail =
      (err && (err.code || err.signal || err.message)) || "unknown error";
    console.warn(
      "gascity mimocode plugin:",
      `${GC_BIN} ${args.join(" ")}`,
      "cwd",
      directory,
      "failed:",
      detail,
    );
  } catch {
    return;
  }
}

function logRunStderr(stderr) {
  try {
    const detail = String(stderr || "").trim();
    if (detail) {
      console.warn("gascity mimocode plugin:", detail);
    }
  } catch {
    return;
  }
}

function unwrapData(result) {
  if (result && typeof result === "object" && "data" in result) {
    return result.data;
  }
  return result;
}

function safeSessionID(sessionID) {
  return String(sessionID || "").replace(/[^A-Za-z0-9_.-]/g, "_");
}

function sessionIDFromEvent(event) {
  return (
    event?.properties?.sessionID ||
    event?.properties?.info?.sessionID ||
    event?.properties?.message?.info?.sessionID ||
    ""
  );
}

function providerSessionEnv(sessionID) {
  sessionID = String(sessionID || "");
  const env = { GC_PROVIDER_SESSION_ID_REQUIRED: "mimocode" };
  if (!sessionID) {
    return env;
  }
  env.GC_PROVIDER_SESSION_ID = sessionID;
  return env;
}

// Gas City's transcript discovery (sessionlog.DefaultMimoCodeSearchPaths)
// reads ~/.local/share/gascity/mimocode-transcripts, so mirroring defaults
// to that path; GC_MIMOCODE_TRANSCRIPT_DIR overrides it (test harnesses).
function defaultTranscriptDir() {
  const home = os.homedir() || "";
  if (!home) {
    return "";
  }
  return path.join(home, ".local", "share", "gascity", "mimocode-transcripts");
}

async function mirrorTranscript(directory, client, sessionID) {
  const exportDir =
    process.env.GC_MIMOCODE_TRANSCRIPT_DIR || defaultTranscriptDir();
  const safeID = safeSessionID(sessionID);
  if (!exportDir || !safeID || !client?.session) {
    return;
  }

  try {
    const [infoResult, messagesResult] = await Promise.all([
      client.session.get({ path: { id: sessionID } }),
      client.session.messages({ path: { id: sessionID } }),
    ]);
    const info = unwrapData(infoResult) || {};
    const messages = unwrapData(messagesResult) || [];
    if (!info.directory) {
      info.directory = directory;
    }
    await fs.mkdir(exportDir, { recursive: true });
    const dst = path.join(exportDir, `${safeID}.json`);
    const tmp = `${dst}.tmp`;
    await fs.writeFile(tmp, JSON.stringify({ info, messages }, null, 2));
    await fs.rename(tmp, dst);
  } catch {
    return;
  }
}

export default async function gascityPlugin({ directory, client }) {
  let cachedPrime = null;
  let injectedPartSeq = 0;

  async function readPrime(force = false, extraEnv = {}) {
    if (force || cachedPrime === null) {
      cachedPrime = await runCommand(directory, ["prime", "--hook"], false, extraEnv);
    }
    return cachedPrime;
  }

  function prependText(existing, prefix) {
    return existing ? prefix + "\n\n" + existing : prefix;
  }

  // buildSystemContext returns only the session-cached gc prime --hook text.
  // Its bytes must not change from turn to turn: the provider's prefix cache
  // keys on the request head, so injecting volatile content here (such as the
  // current-time line that gc nudge drain --inject emits) would evict the
  // entire cached conversation prefix on every request.
  async function buildSystemContext() {
    return await readPrime();
  }

  // buildVolatileInjection returns the per-turn content that must stay out of
  // the cached prefix: the clock line plus queued nudges from
  // gc nudge drain --inject, and unread mail from gc mail check --inject.
  async function buildVolatileInjection() {
    const nudges = await run(directory, "nudge", "drain", "--inject");
    const mail = await run(directory, "mail", "check", "--inject");
    return [nudges, mail].filter(Boolean).join("\n\n");
  }

  // appendVolatileInjection appends the per-turn content to the newest user
  // message, after the stable system prompt and all prior history. Only the
  // new tail bytes are uncached, instead of the whole conversation.
  function appendVolatileInjection(input, output, text) {
    if (!text || !output || !Array.isArray(output.parts)) {
      return;
    }
    const message = output.message || {};
    const sessionID = input?.sessionID || message.sessionID || "";
    const messageID = message.id || input?.messageID || "";
    injectedPartSeq += 1;
    output.parts.push({
      id: `gascity-inject-${Date.now()}-${injectedPartSeq}`,
      sessionID,
      messageID,
      type: "text",
      text,
      synthetic: true,
    });
  }

  return {
    event: async ({ event }) => {
      switch (event.type) {
        case "session.created":
        case "session.compacted":
          {
            const sessionID = sessionIDFromEvent(event);
            await readPrime(true, providerSessionEnv(sessionID));
            await mirrorTranscript(directory, client, sessionID);
          }
          return;
        case "session.idle":
        case "message.updated":
          await mirrorTranscript(directory, client, sessionIDFromEvent(event));
          return;
        default:
          return;
      }
    },

    "chat.message": async (input, output) => {
      const stable = await buildSystemContext();
      if (stable) {
        output.message.system = prependText(output.message.system, stable);
      }
      const volatile = await buildVolatileInjection();
      appendVolatileInjection(input, output, volatile);
    },

    "experimental.chat.system.transform": async (_input, output) => {
      const stable = await buildSystemContext();
      if (stable) {
        if (output.system[0]) {
          output.system[0] = prependText(output.system[0], stable);
        } else {
          output.system.unshift(stable);
        }
      }
    },

    "experimental.session.compacting": async (_input, output) => {
      const handoff = await runWithWarning(directory, "handoff", "--auto", "context cycle");
      if (!handoff) {
        return;
      }
      if (Array.isArray(output?.context)) {
        output.context.push(handoff);
        return;
      }
      try {
        console.warn(
          "gascity mimocode plugin: compacting output.context is not an array; skipped handoff injection",
        );
      } catch {
        return;
      }
    },
  };
}
