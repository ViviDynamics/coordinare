#!/usr/bin/env -S npx -y tsx

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

/**
 * Minimal Codex CLI JSONL client example (TypeScript).
 *
 * Usage:
 *   npx -y tsx docs/examples/codex_exec_json_client.ts "Say OK and nothing else."
 */

type JsonMap = Record<string, unknown>;

type ExecEvent = {
  type?: string;
  item?: { type?: string; text?: string; id?: string };
  usage?: { input_tokens?: number; output_tokens?: number; cached_input_tokens?: number };
  [key: string]: unknown;
};

async function main(): Promise<void> {
  const prompt = process.argv.slice(2).join(" ").trim() || "Say OK and nothing else.";

  const child = spawn(
    "codex",
    ["exec", "--json", "--skip-git-repo-check", prompt],
    { stdio: ["ignore", "pipe", "pipe"] },
  );

  const stdout = child.stdout;
  const stderr = child.stderr;
  if (!stdout || !stderr) {
    throw new Error("failed to capture codex process stdio");
  }

  const rl = createInterface({ input: stdout });
  let sawAssistantMessage = false;

  rl.on("line", (line) => {
    let event: ExecEvent;
    try {
      event = JSON.parse(line) as ExecEvent;
    } catch {
      // Non-JSON line; pass through for diagnostics.
      process.stderr.write(`[codex-nonjson] ${line}\n`);
      return;
    }

    if (event.type === "item.completed" && event.item?.type === "agent_message") {
      sawAssistantMessage = true;
      process.stdout.write(`${event.item.text ?? ""}\n`);
      return;
    }

    if (event.type === "turn.completed" && event.usage) {
      const usage = event.usage;
      process.stderr.write(
        `[usage] input=${usage.input_tokens ?? 0} output=${usage.output_tokens ?? 0} cached=${usage.cached_input_tokens ?? 0}\n`,
      );
    }
  });

  stderr.on("data", (chunk) => {
    const text = chunk.toString();
    if (text.trim().length > 0) {
      process.stderr.write(`[codex-stderr] ${text}`);
    }
  });

  const exitCode: number = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code) => resolve(code ?? 1));
  });

  if (exitCode !== 0) {
    throw new Error(`codex exec exited with code ${exitCode}`);
  }

  if (!sawAssistantMessage) {
    throw new Error("no assistant message was produced");
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
