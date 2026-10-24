/**
 * Akashic Aurora -> OpenCode adapter.
 *
 * OpenCode has no Claude-shaped hooks.json, so this plugin supplies the same seams with its
 * own hook API and delegates every decision to `agent/harness/hooks/opencode_hook.py` (the
 * explicit OpenCode boundary, mirroring `codex_common.py`). Nothing about recall, scope, the
 * injection ledger or identity is re-implemented here: this file only translates.
 *
 * Tier map (see agent/harness/registry.py `opencode`):
 *   T2 boot whisper     session "context" hook, once per session, injected into the system parts
 *   T5 plan recall      session "prompt" hook captures the prompt; next context call injects it
 *   T3 action recall    tool "execute.before" cannot inject, so recall rides the NEXT context
 *                       call -- one beat late, the same honest tier as Cursor
 *   T4 outcome credit   tool "execute.after" carries completed/error directly (no transcript)
 *   T6 close            session.idle after a quiet window writes the last-session draft
 *
 * COST. A cold hook is ~0.2 s (interpreter + imports) plus its work. The plugin therefore keeps
 * ONE warm `--serve` process and sends it JSON lines; a cold spawn is only the fallback. Every
 * request is bounded, and the Python side bounds each seam again, so an unhealthy bus becomes
 * silence instead of a stalled agent.
 *
 * FAIL-OPEN: every call is bounded and every failure degrades to silence. A memory system
 * must never break the session it is helping.
 */

import { Plugin } from "@opencode/plugin"
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import { existsSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const HERE = dirname(fileURLToPath(import.meta.url))
/** agent/harness/plugins/opencode/index.ts -> repo root */
const REPO = process.env.AKASHIC_REPO || resolve(HERE, "../../../..")
const SCRIPT = resolve(REPO, "agent/harness/hooks/opencode_hook.py")

/** Per-event budget. Slightly above the Python deadline so its own answer wins the race. */
const TIMEOUTS: Record<string, number> = {
  SessionStart: 9_000,
  UserPromptSubmit: 6_000,
  PreToolUse: 2_500,
  PostToolUse: 2_500,
  SessionEnd: 6_000,
}

type HookResult = { context?: string; deny?: string; targets?: string[] }

function python(): string {
  const venv = resolve(REPO, ".venv/bin/python")
  if (existsSync(venv)) return venv
  return process.env.AURORA_PYTHON || "python3"
}

function childEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    AKASHIC_HARNESS: "opencode",
    AKASHIC_AGENT_ID: process.env.AKASHIC_AGENT_ID || "opencode",
  }
}

/** Cold path: one process per event. Used when the warm worker is unavailable. */
function runOnce(event: string, payload: Record<string, unknown>): Promise<HookResult> {
  return new Promise((done) => {
    let settled = false
    const finish = (value: HookResult) => {
      if (settled) return
      settled = true
      done(value)
    }
    if (!existsSync(SCRIPT)) return finish({})
    let child: ChildProcessWithoutNullStreams
    try {
      child = spawn(python(), [SCRIPT, "--event", event], {
        cwd: typeof payload.cwd === "string" ? payload.cwd : REPO,
        env: childEnv(),
        stdio: ["pipe", "pipe", "pipe"],
      })
    } catch {
      return finish({})
    }
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL")
      } catch {}
      finish({})
    }, (TIMEOUTS[event] ?? 3_000) + 1_000)
    let out = ""
    child.stdout?.on("data", (chunk) => {
      out += String(chunk)
    })
    child.stderr?.on("data", () => {})
    child.on("error", () => {
      clearTimeout(timer)
      finish({})
    })
    child.on("close", () => {
      clearTimeout(timer)
      try {
        const line = out.trim().split("\n").filter(Boolean).pop()
        const parsed = line ? JSON.parse(line) : {}
        finish(typeof parsed === "object" && parsed ? parsed : {})
      } catch {
        finish({})
      }
    })
    try {
      child.stdin.end(JSON.stringify(payload))
    } catch {
      clearTimeout(timer)
      finish({})
    }
  })
}

// --------------------------------------------------------------------------- warm worker
let worker: ChildProcessWithoutNullStreams | null = null
let workerBuffer = ""
let seq = 0
const inflight = new Map<number, (result: HookResult) => void>()

function failInflight() {
  for (const finish of inflight.values()) finish({})
  inflight.clear()
}

function stopWorker() {
  const child = worker
  worker = null
  workerBuffer = ""
  failInflight()
  if (!child) return
  try {
    child.stdin.end()
    child.kill("SIGKILL")
  } catch {}
}

function ensureWorker(): ChildProcessWithoutNullStreams | null {
  if (worker) return worker
  if (!existsSync(SCRIPT)) return null
  try {
    const child = spawn(python(), [SCRIPT, "--serve"], {
      cwd: REPO,
      env: childEnv(),
      stdio: ["pipe", "pipe", "pipe"],
    })
    child.stdout.on("data", (chunk) => {
      workerBuffer += String(chunk)
      let index = workerBuffer.indexOf("\n")
      while (index >= 0) {
        const line = workerBuffer.slice(0, index).trim()
        workerBuffer = workerBuffer.slice(index + 1)
        if (line) {
          try {
            const msg = JSON.parse(line)
            const finish = inflight.get(msg.id)
            if (finish) {
              inflight.delete(msg.id)
              finish(msg.result && typeof msg.result === "object" ? msg.result : {})
            }
          } catch {}
        }
        index = workerBuffer.indexOf("\n")
      }
    })
    child.stderr.on("data", () => {})
    child.on("error", () => stopWorker())
    child.on("exit", () => {
      if (worker === child) stopWorker()
    })
    worker = child
    return child
  } catch {
    return null
  }
}

function runWarm(event: string, payload: Record<string, unknown>): Promise<HookResult> {
  const child = ensureWorker()
  if (!child?.stdin?.writable) return runOnce(event, payload)
  const id = ++seq
  return new Promise((done) => {
    const timer = setTimeout(() => {
      // A wedged worker cannot answer; drop it so the next call starts clean.
      if (inflight.delete(id)) {
        stopWorker()
        done({})
      }
    }, TIMEOUTS[event] ?? 3_000)
    inflight.set(id, (result) => {
      clearTimeout(timer)
      done(result)
    })
    try {
      child.stdin.write(JSON.stringify({ id, event, payload }) + "\n")
    } catch {
      clearTimeout(timer)
      inflight.delete(id)
      done({})
    }
  })
}

/** The one entry point every hook uses. */
function runHook(event: string, payload: Record<string, unknown>): Promise<HookResult> {
  if (process.env.AKASHIC_OPENCODE === "0") return Promise.resolve({})
  return runWarm(event, payload).catch(() => ({}))
}

export default Plugin.define({
  id: "akashic-aurora",
  async setup(ctx) {
    const booted = new Set<string>()
    const pending = new Map<string, string[]>()
    const closeTimers = new Map<string, ReturnType<typeof setTimeout>>()
    const closed = new Set<string>()

    const stash = (sid: string, text?: string) => {
      if (!sid || !text) return
      const list = pending.get(sid) ?? []
      list.push(text)
      pending.set(sid, list)
    }
    const drain = (sid: string): string[] => {
      const list = pending.get(sid) ?? []
      pending.delete(sid)
      return list
    }
    const cwd = () => ctx.location?.directory || process.cwd()

    // ---- T6 close (debounced: OpenCode has no session-end event) ----------------
    const scheduleClose = (sid: string) => {
      if (!sid || closed.has(sid)) return
      const existing = closeTimers.get(sid)
      if (existing) clearTimeout(existing)
      const timer = setTimeout(
        () => {
          closeTimers.delete(sid)
          if (closed.has(sid)) return
          closed.add(sid)
          void runHook("SessionEnd", { session_id: sid, cwd: cwd() })
        },
        10 * 60 * 1000,
      )
      try {
        ;(timer as any).unref?.()
      } catch {}
      closeTimers.set(sid, timer)
    }

    // ---- T2 boot whisper + T3/T5 deferred context -------------------------------
    await ctx.session.hook("context", async (event) => {
      const sid = String((event as any).sessionID ?? "")
      if (!sid) return
      if (!booted.has(sid)) {
        booted.add(sid)
        const boot = await runHook("SessionStart", { session_id: sid, cwd: cwd() })
        stash(sid, boot.context)
      }
      for (const text of drain(sid)) {
        try {
          event.system.push({ type: "text", text })
        } catch {}
      }
    })

    // ---- T5 plan-time recall ---------------------------------------------------
    await ctx.session.hook("prompt", async (event) => {
      const sid = String((event as any).sessionID ?? "")
      if (!sid) return
      const prompt = String((event as any).prompt?.text ?? "")
      if (!prompt) return
      const result = await runHook("UserPromptSubmit", { session_id: sid, cwd: cwd(), prompt })
      stash(sid, result.context)
    })

    // ---- T3 recall + veto, T4 outcome ------------------------------------------
    // execute.before/after carry their own sessionID (verified on 2.1.4), so attribution is
    // per-event and never inferred from whichever session spoke last.
    await ctx.tool.hook("execute.before", async (event) => {
      const sid = String((event as any).sessionID ?? "")
      if (!sid) return
      const result = await runHook("PreToolUse", {
        session_id: sid,
        cwd: cwd(),
        tool_name: event.tool,
        tool_input: event.input,
      })
      // A house guard (e.g. blanket `git add -A`) is a refusal, not advice: surface it as a
      // tool error so the model sees it and the action does not run.
      if (result.deny) throw new Error(result.deny)
      stash(sid, result.context)
    })

    await ctx.tool.hook("execute.after", async (event) => {
      const sid = String((event as any).sessionID ?? "")
      if (!sid) return
      const result = await runHook("PostToolUse", {
        session_id: sid,
        cwd: cwd(),
        tool_name: (event as any).tool,
        tool_input: (event as any).input,
        tool_status: (event as any).status,
        tool_output: String((event as any).result?.content ?? "").slice(0, 4000),
      })
      stash(sid, result.context)
      if ((event as any).status === "error") scheduleClose(sid)
    })

    // ---- T6 event loop ---------------------------------------------------------
    const controller = new AbortController()
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const type = String((event as any).type ?? "")
          const props = (event as any).properties ?? {}
          const sid = String(props.sessionID ?? props.session?.id ?? props.info?.id ?? "")
          if (!sid) continue
          if (type === "session.idle") scheduleClose(sid)
          else if (type === "session.deleted") {
            closed.add(sid)
            const timer = closeTimers.get(sid)
            if (timer) clearTimeout(timer)
            closeTimers.delete(sid)
            void runHook("SessionEnd", { session_id: sid, cwd: cwd() })
          } else {
            const timer = closeTimers.get(sid)
            if (timer) {
              clearTimeout(timer)
              closeTimers.delete(sid)
            }
          }
        }
      } catch {}
    })()

    // Warm the worker so the first real event does not pay interpreter start.
    ensureWorker()

    return () => {
      controller.abort()
      for (const timer of closeTimers.values()) clearTimeout(timer)
      closeTimers.clear()
      stopWorker()
    }
  },
})
