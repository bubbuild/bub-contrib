# bub-contrib

Contrib packages for the `bub` ecosystem.

## Packages

| Package                                                                              | Bub Plugin Entry Point | Description                                                                                                                               |
| ------------------------------------------------------------------------------------ | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| [`packages/bub-codex`](./packages/bub-codex/README.md)                               | `codex`                | Provides a `run_model` hook that delegates model execution to the Codex CLI.                                                              |
| [`packages/bub-tg-feed`](./packages/bub-tg-feed/README.md)                           | `tg-feed`              | Provides an AMQP-based channel adapter for Telegram feed messages.                                                                        |
| [`packages/bub-schedule`](./packages/bub-schedule/README.md)                         | `schedule`             | Provides scheduling channel/tools backed by APScheduler with a JSON job store.                                                            |
| [`packages/bub-tapestore-sqlalchemy`](./packages/bub-tapestore-sqlalchemy/README.md) | `tapestore-sqlalchemy` | Provides a SQLAlchemy-backed tape store for Bub conversation history.                                                                     |
| [`packages/bub-tapestore-sqlite`](./packages/bub-tapestore-sqlite/README.md)         | `tapestore-sqlite`     | Provides a SQLite-backed tape store for Bub conversation history.                                                                         |
| [`packages/bub-discord`](./packages/bub-discord/README.md)                           | `discord`              | Provides a Discord channel adapter for Bub message IO.                                                                                    |
| [`packages/bub-dingtalk`](./packages/bub-dingtalk/README.md)                         | `dingtalk`             | Provides a DingTalk Stream Mode channel adapter for Bub message IO.                                                                       |
| [`packages/bub-github-copilot`](./packages/bub-github-copilot/README.md)             | `github-copilot`       | Provides a `run_model` hook backed by the GitHub Copilot SDK, plus `bub login github` device-flow login commands.                         |
| [`packages/bub-kimi`](./packages/bub-kimi/README.md)                                 | `kimi`                 | Provides a `run_model` hook backed by the Kimi CLI, including persisted session resume support and temporary Bub skill wiring.            |
| [`packages/bub-qq`](./packages/bub-qq/README.md)                                     | `qq`                   | Provides a QQ Open Platform channel adapter for Bub message IO.                                                                           |
| [`packages/bub-searxng-search`](./packages/bub-searxng-search/README.md)             | `searxng-search`       | Provides a `searxng.search` tool backed by a self-hosted SearXNG instance. Registers the tool only when `BUB_SEARXNG_SEARCH_BASE_URL` is configured. |
| [`packages/bub-web-search`](./packages/bub-web-search/README.md)                     | `web-search`           | Provides a `web.search` tool backed by the Ollama web search API. Registers the tool only when `BUB_SEARCH_OLLAMA_API_KEY` is configured. |
| [`packages/bub-feishu`](./packages/bub-feishu/README.md)                             | `feishu`               | Provides a Feishu channel adapter for Bub message IO.                                                                                     |
| [`packages/bub-session-prompt`](./packages/bub-session-prompt/README.md)             | `session-prompt`       | Provides a session-specific system prompt sourced from `~/.bub/sessions/<session_id>/AGENTS.md`.                                          |
| [`packages/bub-wechat`](./packages/bub-wechat/README.md)                             | `wechat`               | Provides a WeChat channel adapter for Bub message IO.                                                                                     |
| [`packages/bub-wecom`](./packages/bub-wecom/README.md)                               | `wecom`                | Provides a WeCom channel adapter for Bub message IO.                                                                                     |

## Prerequisites

- Python 3.12+ (workspace root)
- `uv` (recommended)

## Usage

To install an individual package, run:

```bash
uv pip install git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/<package-name>
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
