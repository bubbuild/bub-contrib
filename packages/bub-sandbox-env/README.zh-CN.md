# bub-sandbox-env

通过 Bub 的 `provide_environment` hook，让 `bash`、`bash.output`、`bash.kill`、`fs.*` 和 code-mode 的 `run_code` 工具运行在轻量的本地系统沙箱中：macOS 上使用 **Seatbelt**（`sandbox-exec`），Linux 上使用 **bubblewrap**（`bwrap`）。不支持 Windows。

它是 [`bub-docker-env`](../bub-docker-env/README.zh-CN.md) 的轻量版：无需守护进程、无需镜像、没有启动延迟，命令直接使用本机已安装的工具。代价是隔离强度不如容器：进程以你的身份运行在宿主机上，能以只读方式看到你的文件系统。

需要 Bub 0.5 或更高版本。

English documentation: [README.md](./README.md)

## 沙箱允许什么

| | 默认 |
| --- | --- |
| 读 | 整个文件系统，**除了** `deny_read`：`~/.ssh`、`~/.gnupg`、`~/.aws`、`~/.azure`、`~/.config/gcloud`、`~/.kube`、`~/.docker`、`~/.netrc`、`~/.git-credentials`、`~/.npmrc`、`~/.pypirc` 以及 Bub 的 home（`$BUB_HOME`，即 `~/.bub`，其中的配置含 API key） |
| 写 | workspace、每个会话独立的临时目录（`$TMPDIR`；Linux 上为 `/tmp`）、`writable` 中的路径；macOS 上还包括用户级临时和缓存目录（`getconf DARWIN_USER_TEMP_DIR`），`git`、`xcrun` 等工具需要它们 |
| 网络 | 无（`network: true` 开启） |
| 环境变量 | Bub 的环境变量，去掉匹配 `env_deny` 的：`BUB_*`、`*_API_KEY`、`*_TOKEN`、`*_SECRET`、`*_SECRET_KEY`、`*_PASSWORD`、`AWS_*` |

路径重叠时，更具体的路径优先：位于 `~/.bub` 下的 workspace 仍然可用；把 `<workspace>/.env` 加入 `deny_read` 会让这个 `.env` 既不可读也不可写。

各部分运行位置：

- **哪些会话**：默认所有会话。会话 id 形如 `<channel>:<chat id>`，所以 `sessions: ["qq:*"]` 只把 QQ 聊天放进沙箱。
- **沙箱覆盖什么**：进程（`bash`、`run_code`）和文件访问（`fs.*` 也经过同一个沙箱，工具和 shell 看到的是同一套策略）。工具语义保留在 Bub 中。其他工具（渠道工具、`web.fetch`、MCP 工具、其他插件的工具）仍在 Bub 进程中运行，不受沙箱约束。
- **停止**：和 Bub 的宿主环境一样，每个进程都在独立的 session 中启动，所以超时和 `bash.kill` 会结束整个进程树。
- **失败即拒绝**：后端无法运行（没有 `sandbox-exec` 或 `bwrap`）时工具调用直接失败，绝不会回退为不受沙箱约束地执行。

## 配置

写在 `~/.bub/config.yml` 的 `sandbox_env:` 段，或使用环境变量 `BUB_SANDBOX_ENV_<KEY>`（例如 `BUB_SANDBOX_ENV_NETWORK=true`；列表用 JSON：`BUB_SANDBOX_ENV_WRITABLE='["~/.cache/uv"]'`）。修改后需重启 Bub。

```yaml
sandbox_env:
  sessions: ["qq:*"]
  network: false
  writable:
    - ~/.cache/uv
    - ~/.npm
```

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `sessions` | `["*"]` | 会话 id glob 列表；其余会话使用 Bub 的宿主环境。空列表不匹配任何会话 |
| `backend` | `auto` | `seatbelt`、`bwrap`，或 `auto`（macOS 用 Seatbelt，其他平台用 bubblewrap） |
| `network` | `false` | 是否允许网络访问 |
| `writable` | `[]` | 额外可写路径，例如包缓存（`~/.cache/uv`、`~/.npm`） |
| `deny_read` | 见上文 | 禁止读取的路径，支持 `~` 和 `$BUB_HOME`。设置后会替换默认列表 |
| `env_deny` | 见上文 | 不区分大小写的环境变量名 glob，匹配的变量不会传入沙箱 |
| `bwrap` | `bwrap` | bubblewrap 可执行文件 |

## 平台说明

**macOS。** `sandbox-exec` 随系统自带。它被标记为 deprecated，但 Codex 和 Claude Code 也用它做同样的事。profile 默认拒绝一切，只放行普通命令行工具需要的系统服务，因此 `open` 和 AppleScript 无法触达其他应用。

**Linux。** 需要安装 bubblewrap（`apt install bubblewrap`、`dnf install bubblewrap`）。每个进程看到只读的 `/`、由会话临时目录支撑的私有 `/tmp`、独立的 PID 和 IPC 命名空间；除非开启 `network`，否则处于独立的网络命名空间中无法联网。非特权 bubblewrap 依赖 user namespace：Ubuntu 23.10+ 的 AppArmor 可能会阻止，参见 `kernel.apparmor_restrict_unprivileged_userns`。在 Docker 中运行时，容器通常需要 `--security-opt seccomp=unconfined --security-opt apparmor=unconfined`（或 `--privileged`）。

## 局限

- 它防的是误操作和随意滥用，不是对抗蓄意攻击者的硬边界。面向不可信用户时请优先使用 `bub-docker-env`，最好配合 gVisor。
- `deny_read` 之外的所有内容都可读。请把其他敏感信息放在不可读的位置，或加入 `deny_read`。
- workspace 按设计是可写的，命令仍然可以删除其中的一切。
- 没有 CPU、内存或进程数限制。
- macOS 上用户级临时目录与你的其他程序以及其他沙箱会话共享。
