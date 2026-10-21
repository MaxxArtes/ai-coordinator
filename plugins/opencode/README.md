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

`create_task` can receive an `ai_memory_workstream_id`; if omitted, the CLI records `AI_MEMORY_WORKSTREAM_ID` when that variable is present. The plugin does not expose the benchmark command as a tool because a benchmark can schedule several external model calls. Run it deliberately from the CLI with an explicit `--max-calls` limit.

## GitHub Packages

The npm release is published to `https://npm.pkg.github.com` as `@maxxartes/ai-coordinator-opencode` when a version tag passes the release workflow. The workflow also accepts a tag through `workflow_dispatch`, so an existing GitHub Release can be published without creating it again.

To install from GitHub Packages locally, create a classic GitHub personal access token with `read:packages`. Authenticate interactively so the token stays in your user npm configuration, outside this repository. The repository `.npmrc` routes the `@maxxartes` scope to GitHub Packages.

```console
npm login --scope=@maxxartes --auth-type=legacy --registry=https://npm.pkg.github.com
npm view @maxxartes/ai-coordinator-opencode --registry=https://npm.pkg.github.com
npm install @maxxartes/ai-coordinator-opencode@0.4.0 --registry=https://npm.pkg.github.com
```

GitHub Packages creates a newly published npm package as private by default. This package links to the public source repository, so it inherits that repository’s read permissions while keeping its own visibility setting. To restrict access to selected accounts, disable inherited permissions in the package settings and grant explicit access. After authentication, point the existing OpenCode V2 plugin entry at `@maxxartes/ai-coordinator-opencode@0.4.0`; keep its `options` values for the CLI command and shared `dataDir` so the installation continues using the same coordinator database.
