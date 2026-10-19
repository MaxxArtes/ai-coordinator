import { Plugin } from "@opencode/plugin"
import { execFile } from "node:child_process"
import { randomUUID } from "node:crypto"
import { mkdtemp, readFile, rmdir, unlink, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { promisify } from "node:util"
import { fileURLToPath } from "node:url"

const execFileAsync = promisify(execFile)

type PluginOptions = {
  command?: string
  dataDir?: string
  timeoutMs?: number
}

type Input = Record<string, unknown>

function stringValue(input: Input, name: string, fallback?: string): string {
  const value = input[name]
  if (typeof value === "string" && value.trim()) return value
  if (fallback) return fallback
  throw new Error(`Missing required input: ${name}`)
}

function positiveInteger(input: Input, name: string, fallback: number): number {
  const value = input[name]
  if (value === undefined) return fallback
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
    throw new Error(`${name} must be a positive integer`)
  }
  return value
}

const projectSchema = {
  type: "object",
  properties: { project_directory: { type: "string", description: "Absolute path to the current project" } },
  required: ["project_directory"],
  additionalProperties: false,
}

export default Plugin.define({
  id: "ai-coordinator",
  async setup(ctx) {
    const options = ctx.options as PluginOptions
    const command = options.command || "ai-coordinator"
    const dataDir = options.dataDir
    const timeout = options.timeoutMs || 180_000
    const run = async (args: string[]) => {
      const prefix = dataDir ? ["--root", dataDir] : []
      const env = dataDir ? { ...process.env, AI_COORDINATOR_HOME: dataDir } : process.env
      try {
        const { stdout } = await execFileAsync(command, [...prefix, ...args], {
          env,
          windowsHide: true,
          timeout,
          maxBuffer: 2 * 1024 * 1024,
          encoding: "utf8",
        })
        return JSON.parse(stdout.trim()) as unknown
      } catch (error) {
        const failure = error as Error & { stdout?: string; stderr?: string }
        if (failure.stdout) {
          let parsed: unknown
          try {
            parsed = JSON.parse(failure.stdout.trim()) as unknown
          } catch {
            parsed = undefined
          }
          if (parsed !== undefined) {
            if (typeof parsed === "object" && parsed !== null && "error" in parsed) {
              const message = (parsed as { error?: unknown }).error
              if (typeof message === "string") throw new Error(message)
            }
            return parsed
          }
        }
        throw new Error(failure.message || failure.stderr || "ai-coordinator command failed")
      }
    }

    const withTempFile = async <T>(suffix: string, contents: string, action: (path: string) => Promise<T>) => {
      const directory = await mkdtemp(join(tmpdir(), "ai-coordinator-"))
      const path = join(directory, `${randomUUID()}${suffix}`)
      try {
        await writeFile(path, contents, { encoding: "utf8", flag: "wx" })
        return await action(path)
      } finally {
        await unlink(path).catch(() => undefined)
        await rmdir(directory).catch(() => undefined)
      }
    }

    const output = (value: unknown) => ({ content: JSON.stringify(value, null, 2) ?? "null" })

    await ctx.tool.transform((editor) => {
      editor.namespace({
        name: "ai_coordinator",
        description: "Shared task state, checkpoints, and bounded AI worker routes",
      })
      editor.add({
        name: "bootstrap",
        description: "Read the shared protocol and active tasks for a project directory.",
        input: projectSchema,
        options: { namespace: "ai_coordinator" },
        execute: async (input) => output(await run(["bootstrap", "--project", stringValue(input as Input, "project_directory")])),
      })
      editor.add({
        name: "context",
        description: "List active shared tasks for a project directory.",
        input: projectSchema,
        options: { namespace: "ai_coordinator" },
        execute: async (input) => output(await run(["context", "--project", stringValue(input as Input, "project_directory")])),
      })
      editor.add({
        name: "create_task",
        description: "Create a task in the shared coordinator database.",
        input: {
          type: "object",
          properties: {
            project_directory: { type: "string" },
            goal: { type: "string", maxLength: 1500 },
          },
          required: ["project_directory", "goal"],
          additionalProperties: false,
        },
        options: { namespace: "ai_coordinator" },
        execute: async (input) => {
          const value = input as Input
          return output(await run(["new", "--project", stringValue(value, "project_directory"), "--goal", stringValue(value, "goal")]))
        },
      })
      editor.add({
        name: "resume_task",
        description: "Read one task checkpoint and recent worker runs.",
        input: { type: "object", properties: { task_id: { type: "string" } }, required: ["task_id"], additionalProperties: false },
        options: { namespace: "ai_coordinator" },
        execute: async (input) => output(await run(["resume", "--task", stringValue(input as Input, "task_id")])),
      })
      editor.add({
        name: "save_checkpoint",
        description: "Save a checkpoint using the revision from the latest resume result.",
        input: {
          type: "object",
          properties: {
            task_id: { type: "string" },
            revision: { type: "integer", minimum: 1 },
            patch: { type: "object" },
            status: { type: "string", enum: ["active", "complete", "blocked"] },
          },
          required: ["task_id", "revision", "patch"],
          additionalProperties: false,
        },
        options: { namespace: "ai_coordinator" },
        execute: async (input) => {
          const value = input as Input
          const taskId = stringValue(value, "task_id")
          const revision = positiveInteger(value, "revision", 0)
          const patch = value.patch
          if (!patch || typeof patch !== "object" || Array.isArray(patch)) throw new Error("patch must be an object")
          const args = ["checkpoint", "--task", taskId, "--revision", String(revision)]
          if (typeof value.status === "string") args.push("--status", value.status)
          return output(await withTempFile(".json", JSON.stringify(patch), (path) => run([...args, "--file", path])))
        },
      })
      editor.add({
        name: "plan_worker",
        description: "Inspect one configured route and current client catalog without calling a worker.",
        input: { type: "object", properties: { category: { type: "string" }, refresh: { type: "boolean" } }, required: ["category"], additionalProperties: false },
        options: { namespace: "ai_coordinator" },
        execute: async (input) => {
          const value = input as Input
          const args = ["plan", "--category", stringValue(value, "category")]
          if (value.refresh === true) args.push("--refresh")
          return output(await run(args))
        },
      })
      editor.add({
        name: "delegate_to_worker",
        description: "Call configured external AI clients. May consume quota or balance; output is a proposal to review.",
        input: {
          type: "object",
          properties: {
            task_id: { type: "string" },
            category: { type: "string" },
            input_text: { type: "string", maxLength: 8000 },
            max_attempts: { type: "integer", minimum: 1, maximum: 2 },
            response_format: { type: "string", enum: ["text", "json", "python", "code"] },
          },
          required: ["task_id", "category", "input_text"],
          additionalProperties: false,
        },
        options: { namespace: "ai_coordinator" },
        execute: async (input) => {
          const value = input as Input
          const args = [
            "run", "--task", stringValue(value, "task_id"),
            "--category", stringValue(value, "category"), "--input",
          ]
          const attempts = positiveInteger(value, "max_attempts", 1)
          if (attempts > 2) throw new Error("max_attempts must be 1 or 2")
          args.push("--max-attempts", String(attempts))
          if (typeof value.response_format === "string") args.push("--response-format", value.response_format)
          return output(await withTempFile(".md", stringValue(value, "input_text"), (path) => run([...args, path])))
        },
      })
    })

    const moduleDir = dirname(fileURLToPath(import.meta.url))
    const skillPath = resolve(moduleDir, "..", "skills", "ai-coordinator", "SKILL.md")
    const skillContent = await readFile(skillPath, "utf8")
    await ctx.skill.transform((editor) => {
      editor.add({
        id: "ai-coordinator",
        name: "AI Coordinator",
        description: "Retoma tarefas compartilhadas, grava checkpoints e coordena workers de IA.",
        path: skillPath,
        content: skillContent,
      })
    })
  },
})
