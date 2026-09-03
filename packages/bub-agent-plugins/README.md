# bub-agent-plugins

Load [Agent Plugins 1.0.0](https://agent-plugins.org/specification) packages into Bub without
replacing Bub's existing plugin, skill, or MCP behavior.

The plugin supports both portable component types:

- `skills/*/SKILL.md` is validated with Bub's existing Agent Skills parser and added to its normal
  discovery namespace. Project and user skills keep their existing precedence.
- `mcp.json` is validated and adapted to a read-only `bub-mcp` lifecycle channel. Stdio,
  Streamable HTTP, and legacy SSE transports are supported.

Invalid manifests reject only that plugin. Invalid skills, MCP files, server entries, or connection
attempts stay isolated at the failure boundary required by the specification.

## Installation

```bash
bub install bub-mcp@main
bub install bub-agent-plugins@main
```

`bub-mcp>=0.2.0` is a runtime requirement, but intentionally not a transitive package dependency,
matching the repository convention that Bub host capabilities are installed explicitly into the
same environment. Its existing `~/.bub/mcp.json` configuration remains independent.

## Discovery and configuration

By default, immediate child directories are discovered under:

- `<workspace>/.agents/plugins/`
- `~/.agents/plugins/`

Exact plugin roots can also be configured in `~/.bub/config.yml`:

```yaml
agent-plugins:
  paths:
    - /opt/agent-plugins/reporting
  auto_discover: true
  skills_enabled: true
  mcp_enabled: true
  data_root: ~/.bub/agent-plugins
```

Relative configured paths resolve against Bub's active workspace. For environment-based settings,
use `BUB_AGENT_PLUGINS_*`; list values such as `PATHS` use Pydantic's JSON syntax.

`skills_enabled` and `mcp_enabled` are independent feature gates for controlled rollouts.

## Portable package layout

```text
reporting/
├── plugin.json
├── skills/
│   └── summarize/
│       └── SKILL.md
└── mcp.json
```

Stdio servers receive absolute `PLUGIN_ROOT` and persistent `PLUGIN_DATA` environment variables.
The two placeholders are expanded only in `args`, `env` values, and `cwd`, as required by Agent
Plugins 1.0.0. MCP tool names are namespaced as `mcp.<plugin>.<server>_<tool>`.

The package does not install or update Agent Plugin directories; distribution is intentionally left
to the host or deployment workflow.
