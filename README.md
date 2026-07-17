# bub-contrib

Some contrib packages for the `bub` ecosystem.

## Plugin Discovery

You can find a broader plugin catalog at [hub.bub.build](https://hub.bub.build), which includes packages maintained in this repository as well as third-party Bub plugins.

If you have developed a plugin, you can also choose to register it in [`bubbuild/buildscape`](https://github.com/bubbuild/buildscape) instead of submitting all of its source code to this repository.

Below is the list of packages currently included in this repository.

<details>
<summary>Packages In This Repository</summary>

| Package                                                                     | PyPI Status                                                                                               | Description                                                                                                                                   |
| --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| [`bub-codex`](./packages/bub-codex/README.md)                               |                                                                                                           | Provides a `run_model` hook that delegates model execution to the Codex CLI.                                                                  |
| [`bub-cursor`](./packages/bub-cursor/README.md)                             |                                                                                                           | Provides a `run_model` hook that delegates model execution to the Cursor CLI, plus `bub login cursor`.                                        |
| [`bub-acp-server`](./packages/bub-acp-server/README.md)                     |                                                                                                           | Exposes Bub as an Agent Client Protocol agent with `bub acp serve` for ACP-compatible editors.                                                |
| [`bub-schedule`](./packages/bub-schedule/README.md)                         | [![PyPI version](https://img.shields.io/pypi/v/bub-schedule)](https://pypi.org/project/bub-schedule/)     | Provides scheduling channel/tools backed by APScheduler with a JSON job store.                                                                |
| [`bub-tapestore-otel`](./packages/bub-tapestore-otel/README.md)             |                                                                                                           | Wraps the active tape store and projects committed tape writes to OpenTelemetry through Logfire.                                              |
| [`bub-tapestore-sqlalchemy`](./packages/bub-tapestore-sqlalchemy/README.md) |                                                                                                           | Provides a SQLAlchemy-backed tape store for Bub conversation history.                                                                         |
| [`bub-tapestore-sqlite`](./packages/bub-tapestore-sqlite/README.md)         |                                                                                                           | Provides a SQLite-backed tape store for Bub conversation history.                                                                             |
| [`bub-discord`](./packages/bub-discord/README.md)                           | [![PyPI version](https://img.shields.io/pypi/v/bub-discord)](https://pypi.org/project/bub-discord/)       | Provides a Discord channel adapter for Bub message IO.                                                                                        |
| [`bub-dingtalk`](./packages/bub-dingtalk/README.md)                         |                                                                                                           | Provides a DingTalk Stream Mode channel adapter for Bub message IO.                                                                           |
| [`bub-extism`](./packages/bub-extism/README.md)                             |                                                                                                           | Bridges selected Bub hooks to Extism WebAssembly plugins so extensions can be written in any Extism PDK language.                             |
| [`bub-github-copilot`](./packages/bub-github-copilot/README.md)             |                                                                                                           | Provides a `run_model` hook backed by the GitHub Copilot SDK, plus `bub login github` device-flow login commands.                             |
| [`bub-kimi`](./packages/bub-kimi/README.md)                                 |                                                                                                           | Provides a `run_model` hook backed by the Kimi CLI, including persisted session resume support and temporary Bub skill wiring.                |
| [`bub-mcp`](./packages/bub-mcp/README.md)                                   | [![PyPI version](https://img.shields.io/pypi/v/bub-mcp)](https://pypi.org/project/bub-mcp/)               | Exposes configured MCP servers as Bub tools, with `bub mcp` CLI commands to list, add, and remove server configs.                             |
| [`bub-mcp-server`](./packages/bub-mcp-server/README.md)                     | [![PyPI version](https://img.shields.io/pypi/v/bub-mcp-server)](https://pypi.org/project/bub-mcp-server/) | Exposes Bub as an SSE MCP server with a `run_model` tool.                                                                                     |
| [`bub-qq`](./packages/bub-qq/README.md)                                     |                                                                                                           | Provides a QQ Open Platform channel adapter for Bub message IO.                                                                               |
| [`bub-web-search`](./packages/bub-web-search/README.md)                     | [![PyPI version](https://img.shields.io/pypi/v/bub-web-search)](https://pypi.org/project/bub-web-search/) | Provides provider-selectable Ollama (`web.search`) and SearXNG (`searxng.search`) tools, enabling only the configured search provider.        |
| [`bub-feishu`](./packages/bub-feishu/README.md)                             | [![PyPI version](https://img.shields.io/pypi/v/bub-feishu)](https://pypi.org/project/bub-feishu/)         | Provides a Feishu channel adapter for Bub message IO.                                                                                         |
| [`bub-slack`](./packages/bub-slack/README.md)                               |                                                                                                           | Provides a Slack (Socket Mode) channel adapter for Bub message IO.                                                                            |
| [`bub-session-prompt`](./packages/bub-session-prompt/README.md)             |                                                                                                           | Provides a session-specific system prompt sourced from `~/.bub/sessions/<session_id>/AGENTS.md`.                                              |
| [`tape-dataset-opendal`](./packages/tape-dataset-opendal/README.md)         |                                                                                                           | Exports standard Bub tapes to a backend-agnostic dataset layout through OpenDAL, with CEL filtering and staged share-review support. |
| [`bub-wechat`](./packages/bub-wechat/README.md)                             |                                                                                                           | Provides a WeChat channel adapter for Bub message IO.                                                                                         |
| [`bub-wecom`](./packages/bub-wecom/README.md)                               |                                                                                                           | Provides a WeCom channel adapter for Bub message IO.                                                                                          |

</details>

## Prerequisites

- Python 3.12+ (workspace root)
- `uv` (recommended)

## Usage

To install an individual package, run:

```bash
bub install <package-name>@main
```

## Development Setup

Install all workspace dependencies:

```bash
uv sync
```

## Governance Model

We encourage all plugin contributors to take responsibility for the ongoing maintenance of their submitted plugins. Each plugin should ideally have at least one active maintainer who is familiar with its domain and willing to respond to issues or update dependencies as needed.

To foster a healthy and growing ecosystem, the code review standards for contributed plugins will be appropriately relaxed compared to core Bub repositories. We prioritize:

- **Practicality and usefulness** over strict style or architectural perfection
- **Clear ownership**: contributors are expected to respond to issues and PRs related to their plugins
- **Basic safety and compatibility**: plugins should not break the workspace or introduce security risks

We welcome experimental, niche, or work-in-progress plugins, as long as they are clearly documented and do not negatively impact other packages in this repository.

If you are submitting a plugin, please be prepared to maintain it or help find a new maintainer if you become unavailable.

---
## License

This repository is licensed under [LICENSE](./LICENSE).
