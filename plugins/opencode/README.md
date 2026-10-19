# OpenCode V2 plugin

This package adds the shared `ai-coordinator` skill and tools for bootstrap, active task context, task creation/resume, checkpoints, route planning, and explicit worker delegation.

Install the Python package first so the `ai-coordinator` command is available. Load this directory from `opencode.json`:

```json
{
  "plugins": ["./plugins/opencode"]
}
```

Plugin options can override the CLI and data directory:

```json
{
  "plugins": [
    {
      "package": "./plugins/opencode",
      "options": {
        "command": "ai-coordinator",
        "dataDir": "C:/Users/me/AppData/Local/ai-coordinator"
      }
    }
  ]
}
```

The plugin uses `execFile` with an argument array and does not invoke a shell. Worker delegation calls configured local AI clients and may consume quota or balance.
