# bub-docker-env

通过 Bub 的 `provide_environment` hook，把 `bash`、`bash.output`、`bash.kill`、`fs.*` 以及 code mode 的 `run_code` 放进 Docker 容器里执行，而不是直接在宿主机上运行。适用于不完全可信的人也能让 agent 执行命令的场景，比如聊天渠道。

需要 Bub 0.5 及以上。旧版 Bub 会忽略这个 hook，命令仍在宿主机上执行，所以本包在旧版上会拒绝安装。

English documentation: [README.md](./README.md)

## 什么在哪里运行

- **哪些会话**：默认所有会话。会话 id 的格式是 `<渠道>:<聊天 id>`，所以设置 `sessions: "qq:*"` 就只让 QQ 聊天进容器，你自己的 `bub chat` / `bub run` 仍在宿主机执行。
- **哪些操作进容器**：进程（`bash`、`run_code`）和文件访问（`fs.*`）。工具语义仍由 Bub 负责：超时、后台 shell、输出渲染和 hook 都不变。其他工具（渠道自己的工具、`web.fetch`、MCP 工具、其他插件的工具）仍在宿主机上的 Bub 进程里运行。
- **容器能看到什么**：Bub 工作区，**按原路径**挂载（Bub 会在 prompt 里告诉模型工作区路径，所以容器里必须存在同一路径）；以及工作区之外的 skill 目录（`~/.agents/skills` 和已安装插件自带的 skill），**只读、按宿主机原路径**挂载，这样 skill 让模型执行的脚本在容器里也能找到。除此之外看不到宿主机上的任何东西。密钥（`~/.bub/config.yml`、API key、状态文件）请放在工作区之外。
- **能改动什么**：只有工作区和 `/tmp`。工作区本身必须可写，所以被批准的命令仍然可以删除其中的全部内容：不要在工作区里存放不可替代的文件。

每个容器启动时的默认设置：

| 项目 | 默认值 |
| --- | --- |
| 网络 | 完全断网 |
| 用户 | 宿主机当前用户的 uid:gid，工作区里的文件归属不变 |
| 权限 | 去掉全部 capabilities，并开启 `no-new-privileges` |
| 资源限制 | 内存 1 GB、1 个 CPU、256 个进程 |
| 根文件系统 | 只读；`/tmp` 是 512 MB 的 tmpfs；`HOME` 为 `/tmp`，写到 `~` 下的内容会随容器删除而消失 |
| 镜像 | `python:3.12-slim` |

## 生命周期

- 会话的容器在第一次需要它的工具调用（`bash`、`fs.*` 或 `run_code`）时启动，耗时一两秒；首次使用时会拉取镜像。
- 容器投入使用前，插件会比较工作区里的文件名和容器内工作区路径下看到的内容，一个都对不上就拒绝并删除这个容器，见[部署注意事项](#部署注意事项)。
- Bub 退出时删除容器。`scope: shared` 时，唯一的容器在最后一个使用它的会话关闭时删除，这同样发生在 Bub 退出时。
- Docker 不可用时工具调用直接失败，命令不会退回到宿主机执行。
- 每个进程在容器里以独立的会话启动并记录进程组，超时和 `bash.kill` 会结束整个进程树，与宿主机上的行为一致。

容器命名为 `bub-env-<12 位十六进制>`，并带有标签 `bub.docker-env=1` 和 `bub.docker-env.key=<会话 id，或 "shared">`。

## 配置

写在 `~/.bub/config.yml` 的 `docker_env:` 段中，或使用环境变量 `BUB_DOCKER_ENV_<KEY>`（例如 `BUB_DOCKER_ENV_NETWORK=bridge`）。Bub 在启动时读取配置，修改后需要重启 Bub。

```yaml
docker_env:
  sessions: "qq:*"
  network: none
```

| 配置项 | 默认值 | 可选值 | 作用 |
| --- | --- | --- | --- |
| `sessions` | `"*"` | 逗号分隔的会话 id glob，例如 `"qq:*"`、`"qq:*,feishu:*"`；留空则不匹配任何会话 | 哪些会话进容器，其余会话使用 Bub 的宿主机环境 |
| `scope` | `session` | `session`、`shared` | `session`：每个会话一个容器。`shared`：所有匹配的会话共用一个容器，更省资源，但会话之间能看到彼此的进程和 `/tmp` |
| `image` | `python:3.12-slim` | 任意镜像 | 需要提供 `bash`、`sh`、`setsid`（util-linux）、`cat`、`mkdir`、`dirname` 和 `ls`；只有 `run_code` 需要 `python3` |
| `network` | `none` | `none`、`bridge` 或某个 Docker 网络名 | `bridge` 会让命令既能访问外网，**也能访问你的局域网** |
| `memory` | `1g` | Docker 内存大小，例如 `512m`、`2g` | 对应 `docker run --memory` |
| `cpus` | `"1"` | CPU 数，例如 `"0.5"`、`"2"` | 对应 `docker run --cpus` |
| `pids_limit` | `256` | 整数，至少 16 | 对应 `docker run --pids-limit` |
| `user` | `""` | `""`、`"uid:gid"`、`none` | `""`：使用宿主机当前用户的 uid:gid。`none`：不传 `--user`（rootless Docker、Podman） |
| `workdir` | `""` | `""` 或绝对路径 | 工作区在容器里的位置。`""`：使用工作区自身的路径（推荐） |
| `read_only` | `true` | `true`、`false` | 只读根文件系统，`/tmp` 使用 tmpfs。设为 `false` 后根文件系统可写，但仅靠这一项通常仍无法安装软件，见[添加工具](#添加工具) |
| `tmp_size` | `512m` | Docker tmpfs 大小，例如 `256m`、`1g` | `read_only` 开启时 `/tmp` 的大小 |
| `mount_skills` | `true` | `true`、`false` | 以只读方式、按原路径挂载工作区之外的 skill 目录 |
| `host_workspace` | `""` | `""` 或 Docker 宿主机上的绝对路径 | Bub 与 Docker daemon 不在同一文件系统时，填写 daemon 一侧看到的工作区路径。设置后不挂载 skill 目录 |
| `mount_options` | `""` | 挂载选项，例如 `z`、`Z` | 应用到所有绑定挂载；会改用 `--volume` 语法，因此挂载路径中不能含有 `:` |
| `extra_run_args` | `""` | 额外的 `docker run` 参数，按 shell 命令行规则拆分 | 例如 `--runtime=runsc`（gVisor）、`--userns=keep-id`（Podman） |
| `docker` | `docker` | Bub 的 `PATH` 中兼容 Docker 的命令，或其完整路径 | 插件调用的命令行工具 |

### 添加工具

如果命令需要默认镜像里没有的工具（`git`、`jq`、Python 包等），请构建包含这些工具的镜像，而不是在运行时安装：

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git jq \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir requests
```

```bash
docker build -t my-bub-sandbox:1 .
```

```yaml
docker_env:
  image: my-bub-sandbox:1
```

这样容器仍然保持只读、断网、非 root，而且每个新容器里都有这些工具。如果改为在运行时安装，仅设置 `read_only: false` 是不够的：命令以非 root 用户运行且去掉了全部 capabilities，系统目录依旧不可写，包管理器会失败；默认的 `network: none` 也连不上软件源；装好的东西还会随容器删除而丢失。只有在接受隔离程度降低的前提下，才同时放宽 `read_only`、`user` 和 `network`。

## 部署注意事项

Docker daemon 是在**它自己所在的机器上**解析挂载路径的，而不是在 Bub 所在的地方。两者不一致时，工作区路径可能会悄悄挂进一个空目录或无关目录；挂载检查会拒绝这样的容器，报错中会提示设置 `host_workspace`。工作区为空时无法比较，日志里会出现 `docker_env.mount.unverified`。

| 部署方式 | 需要配置 | 实测 |
| --- | --- | --- |
| Bub 直接运行在 Docker 宿主机上 | 无 | macOS + Docker Desktop |
| Bub 运行在容器里并使用宿主机的 `docker.sock`，或使用远程 `DOCKER_HOST` | `host_workspace`：工作区在 Docker 宿主机上的路径 | 仅验证了错误挂载会被拒绝 |
| SELinux（Fedora、RHEL） | `mount_options: z` | 否 |
| rootless Docker | `user: none` | 否 |
| Podman | `docker: podman`、`user: none`、`extra_run_args: "--userns=keep-id"` | 否 |
| gVisor | `extra_run_args: "--runtime=runsc"` | 否 |
| Docker Desktop | 工作区必须位于共享路径下（Settings → Resources → File sharing） | 是 |

支持 Linux 和 macOS 宿主机，不支持 Windows。Kubernetes 集群和托管的 Agent 沙箱通常不提供 Docker daemon，需要使用对接其自身 API 的执行环境插件。

## 日志

| 事件 | 级别 | 含义 |
| --- | --- | --- |
| `docker_env.session` | INFO | 某个会话获得了 Docker 环境（附容器名和 scope） |
| `docker_env.session.host` | DEBUG | 某个会话不匹配 `sessions`，留在宿主机执行 |
| `docker_env.container.started` | INFO | 容器已启动（镜像、网络） |
| `docker_env.container.removed` | INFO | 容器已删除 |
| `docker_env.mount.unverified` | INFO | 工作区为空，挂载检查无法比较 |
| `docker_env.signal_failed` | WARNING | 无法发出停止容器内进程的请求 |

挂载被拒绝或容器启动失败时，会作为工具调用的错误返回。

## 安装

`bub-docker-env` 尚未发布到 PyPI。全局安装 Bub 时：

```bash
bub install bub-docker-env@main
```

本地开发时，把包路径装进运行 `bub` 的同一个环境：

```bash
uv tool install bub --with /path/to/bub-contrib/packages/bub-docker-env
```

需要有正在运行的 Docker（Docker Desktop、OrbStack 或 Docker Engine），并且 Bub 进程的 `PATH` 中能找到 `docker` 命令。

## 清理

Bub 崩溃或被 `kill -9` 后，容器会继续运行，可以这样清理：

```bash
docker rm -f $(docker ps -q --filter label=bub.docker-env=1)
```

插件启动时不会自动清理，因为同一个 Docker 上可能还有其他 Bub 实例在使用自己的容器。
