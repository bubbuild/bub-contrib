# bub-qq

面向 [Bub](https://bub.build) 的 QQ 开放平台 Channel 适配器。

English documentation: [README.md](./README.md)

## 功能概览

| 能力 | 说明 |
| --- | --- |
| 单聊（C2C）收发 | `C2C_MESSAGE_CREATE` 适配为 Bub `ChannelMessage`；被动文本 / markdown 回复 |
| 群聊收发 | `GROUP_AT_MESSAGE_CREATE` / `GROUP_MESSAGE_CREATE`；支持全量消息模式，payload 携带 `was_mentioned` / `sender_role` |
| 群主动消息 | 被动回复不可用时兜底主动发送（`active_messages`，需群管理员在 QQ 客户端授权） |
| 回复模式与选择性沉默 | `reply_mode: direct`（默认）直通转发模型最终文本、`<no_reply/>` 哨兵表示沉默；`reply_mode: tool` 注册原生 `qq.send` 工具，模型调用即回复、不调用即沉默（见「回复模式」） |
| 引用与聊天记录 | 解析 `msg_elements`，将引用消息 / 合并转发内容以 `quoted_messages` 传给模型 |
| 接收模式 | **webhook** 或 **websocket** 二选一（QQ 平台侧互斥）；含 ed25519 验签与断线重连 |
| 安全防护 | 用户/群白名单、按角色门控的逗号命令、按场景工具策略、LLM 频控、审计日志（见「安全」） |
| 平台状态持久化 | 主动消息授权（`*_MSG_RECEIVE` / `*_MSG_REJECT`）与群 claw_cfg 落盘，重启不丢 |
| 可靠发送 | 入站/出站去重、`msg_seq` 管理、错误码目录；异步人工审核（304023/304024）按「待审核成功」处理 |
| Onboarding | `bub onboard` 交互采集 `appid` / `secret` / `receive_mode`；随包提供 skill 资源（`src/skills/qq`） |

插件入口点：`qq` → `bub_qq.plugin`。当前支持 **单聊（C2C）** 和 **群聊** 文本收发，QQ 频道（Guild）尚未覆盖。

## 前置条件

1. [安装 Bub](https://bub.build/zh-cn/docs/getting-started/install/)（推荐：`uv tool install bub`）
2. 执行 `bub onboard`，确认模型可用（`bub chat` 或 `bub run`）
3. 在 [QQ 开放平台 / 机器人文档](https://bot.q.qq.com/wiki/develop/api-v2/) 创建机器人，获取 `APPID`、`SECRET`

## 安装（终端用户）

`bub-qq` 尚未发布到 PyPI。若已用 `uv tool install bub` 全局安装 Bub，请用 **`bub install` 把插件装进 Bub 自己的环境**：

```bash
bub install bub-qq@main
```

该写法会解析为官方 monorepo 子包：

```text
git+https://github.com/bubbuild/bub-contrib.git@main#subdirectory=packages/bub-qq
```

等价写法：

```bash
# 完整 Git URL（传给 bub install 时用 https:// 开头，不要写 git+https://）
bub install "https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-qq"

# 有 tag / commit 时可固定版本
bub install bub-qq@<tag-or-sha>
```

验证是否加载：

```bash
bub hooks
```

应能在已发现的插件 / hook 实现中看到 `qq`。

### 升级 / 卸载

```bash
bub update bub-qq
bub uninstall bub-qq
```

### 说明

- **不要**写裸的 `bub install bub-qq`。不带 `@ref` 的名字会被当成 PyPI 包名。
- `bub install` 要求 Bub 运行在虚拟环境中（包含 `uv tool install bub` 创建的环境），且本机 `PATH` 上有 `uv`。

## 安装（本地开发）

将插件以 **editable** 方式装进「实际运行 `bub` 的那个环境」。

### 方式 A — 全局 Bub（`uv tool`）

```bash
uv pip install -e /path/to/bub-contrib/packages/bub-qq \
  --python ~/.local/share/uv/tools/bub/bin/python
```

之后照常使用全局命令：

```bash
bub hooks
bub gateway
```

### 方式 B — Bub / monorepo 项目 venv

在已依赖 Bub 的 uv 项目中：

```bash
uv add --editable /path/to/bub-contrib/packages/bub-qq
# 或在 bub-contrib 工作流中：
uv pip install -e packages/bub-qq
```

### 方式 C — 指定解释器从 Git 安装

```bash
uv pip install \
  "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-qq" \
  --python /path/to/the/python/that/runs/bub
```

## 配置

配置可来自：

- `~/.bub/config.yml` 中的 `qq:` 段
- `BUB_QQ_*` 环境变量（含从 `.env` 加载的值）
- `bub onboard`：当 `qq` 通道启用时，交互采集必填项

环境变量会覆盖 YAML，因此共享策略可放在 `config.yml`，密钥放在环境变量中。

### 必填

| YAML 字段（`qq.*`） | 环境变量 | 说明 |
| --- | --- | --- |
| `appid` | `BUB_QQ_APPID` | QQ 机器人 AppID |
| `secret` | `BUB_QQ_SECRET` | QQ 机器人 Secret |
| `receive_mode` | `BUB_QQ_RECEIVE_MODE` | 入站传输：`webhook` 或 `websocket` |

`receive_mode` 必须与 QQ 开发者后台一致：

- `webhook` — 仅启动内嵌 webhook 服务，不启 WebSocket
- `websocket` — 仅启动 WebSocket 客户端，不启内嵌 webhook

QQ 侧将 webhook 与 WebSocket 视为 **互斥**。成功配置有效的 HTTPS 回调地址后，平台侧通常不再支持 WebSocket 投递。

若 `appid` / `secret` 为空，或 `receive_mode` 不是 `webhook` / `websocket`，gateway 启动会失败。

### 可选

| YAML 字段（`qq.*`） | 环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `token_url` | `BUB_QQ_TOKEN_URL` | `https://bots.qq.com/app/getAppAccessToken` | 访问令牌接口 |
| `openapi_base_url` | `BUB_QQ_OPENAPI_BASE_URL` | `https://api.bot.qq.com` | OpenAPI 基址（官方统一请求地址；旧地址 `https://api.sgroup.qq.com` 可在此覆盖） |
| `timeout_seconds` | `BUB_QQ_TIMEOUT_SECONDS` | `30` | 令牌与 OpenAPI 的 HTTP 超时 |
| `token_refresh_skew_seconds` | `BUB_QQ_TOKEN_REFRESH_SKEW_SECONDS` | `60` | 到期前多少秒刷新令牌 |
| `webhook_host` | `BUB_QQ_WEBHOOK_HOST` | `127.0.0.1` | 内嵌 webhook 监听地址 |
| `webhook_port` | `BUB_QQ_WEBHOOK_PORT` | `8080` | 内嵌 webhook 端口（QQ 允许 `80` / `443` / `8080` / `8443`） |
| `webhook_path` | `BUB_QQ_WEBHOOK_PATH` | `/qq/webhook` | webhook 路径 |
| `webhook_callback_timeout_seconds` | `BUB_QQ_WEBHOOK_CALLBACK_TIMEOUT_SECONDS` | `15` | 预留给后续回调控制 |
| `verify_signature` | `BUB_QQ_VERIFY_SIGNATURE` | `true` | 是否校验 webhook 签名 |
| `webhook_signature_timestamp_tolerance_seconds` | `BUB_QQ_WEBHOOK_SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS` | `0` | webhook 签名时间戳与本地时间的最大允许偏差（秒），`0` 表示不做时效校验 |
| `inbound_dedupe_size` | `BUB_QQ_INBOUND_DEDUPE_SIZE` | `1024` | 近期入站 `msg_id` 去重缓存大小 |
| `session_state_size` | `BUB_QQ_SESSION_STATE_SIZE` | `1024` | 被动回复会话状态与发送记录的内存条数上限（超出后淘汰最旧条目） |
| `passive_reply_window_seconds` | `BUB_QQ_PASSIVE_REPLY_WINDOW_SECONDS` | `3600` | 入站消息之后尝试被动回复的时间窗口（秒） |
| `active_messages` | `BUB_QQ_ACTIVE_MESSAGES` | `false` | 无法被动回复时改发群主动消息（不带 `msg_id`）；需要群管理员在 QQ 客户端允许机器人主动发言 |
| `passive_replies_per_msg_id` | `BUB_QQ_PASSIVE_REPLIES_PER_MSG_ID` | `4` | 每条入站 `msg_id` 的被动回复本地上限；超出后降级为主动消息（若已开启）或跳过 |
| `reply_mode` | `BUB_QQ_REPLY_MODE` | `direct` | 模型输出如何到达 QQ：`direct` 直通转发最终文本（输出 `<no_reply/>` 表示沉默）；`tool` 关闭直通转发并注册 `qq.send` 工具（见「回复模式」） |
| `state_file` | `BUB_QQ_STATE_FILE` | 空 | 持久化平台开关状态（主动消息授权、群 claw_cfg）的 JSON 文件；留空使用 `<bub home>/qq/state.json` |
| `admin_users` | `BUB_QQ_ADMIN_USERS` | 空 | 逗号分隔的用户 openid，在所有场景拥有完整的逗号命令与工具权限 |
| `allow_users` | `BUB_QQ_ALLOW_USERS` | 空 | 逗号分隔的 C2C 白名单；设置后其他用户的私聊消息会被丢弃 |
| `allow_groups` | `BUB_QQ_ALLOW_GROUPS` | 空 | 逗号分隔的群白名单；设置后其他群的消息会被丢弃 |
| `group_tool_policy` | `BUB_QQ_GROUP_TOOL_POLICY` | `restricted` | 群会话工具策略：`open` / `restricted`（禁用 `bash*`、`fs.write`、`fs.edit`、`subagent`）/ `locked`（禁用全部工具） |
| `c2c_tool_policy` | `BUB_QQ_C2C_TOOL_POLICY` | `open` | C2C 会话工具策略，取值同 `group_tool_policy` |
| `denied_tools` | `BUB_QQ_DENIED_TOOLS` | 空 | `restricted` 策略下额外禁用的工具名 glob，逗号分隔，如 `web.fetch,tape.*` |
| `llm_rate_limit_per_minute` | `BUB_QQ_LLM_RATE_LIMIT_PER_MINUTE` | `0` | 每发送者在每个会话内每分钟的 LLM 调用上限；`0` 表示不限 |
| `llm_rate_limit_notice` | `BUB_QQ_LLM_RATE_LIMIT_NOTICE` | `请求过于频繁，请稍后再试。` | 触发频控时回复的文本 |
| `websocket_intents` | `BUB_QQ_WEBSOCKET_INTENTS` | `1 << 25` | WebSocket identify intents（`GROUP_AND_C2C_EVENT`） |
| `websocket_use_shard_gateway` | `BUB_QQ_WEBSOCKET_USE_SHARD_GATEWAY` | `false` | 是否按 `/gateway/bot` 建议分片数连接 |
| `websocket_reconnect_delay_seconds` | `BUB_QQ_WEBSOCKET_RECONNECT_DELAY_SECONDS` | `5` | WebSocket 断线后重连延迟 |

示例：

```yaml
qq:
  appid: your_app_id
  secret: your_secret
  receive_mode: websocket
```

```bash
export BUB_QQ_APPID=your_app_id
export BUB_QQ_SECRET=your_secret
export BUB_QQ_RECEIVE_MODE=websocket
```

群聊能听到哪些消息，由 QQ 客户端里群管理员的「允许机器人可获取的群聊消息范围」决定（全部消息 / @ 最近 10 条 / 仅 @）。插件对收到的每条群消息都会唤醒模型；未 @ 时 payload 里 `was_mentioned` 为 `false`，模型可按当前回复模式选择不回复。

最新版手机 QQ 中的设置路径：**进入群聊 → 右上角「更多」 → 群机器人 → 管理**。群主或管理员可在此调整「机器人可获取的群聊消息范围」，以及是否「允许机器人主动发言」（配合 `active_messages` 使用）。

## 回复模式

`reply_mode` 决定模型输出如何变成 QQ 消息，以及同样重要的——模型如何保持沉默（例如群聊中未被 @ 且无话可说时）。用 opt-in/opt-out 的语言说：`direct` 是 **opt-out**（默认回复，模型输出哨兵显式退出本轮）；`tool` 是 **opt-in**（默认沉默，模型调用发送工具显式选择回复——与 Bub 其他通道的契约一致）。两种模式共享同一条发送链路（被动 `msg_id`/`msg_seq` 定位、去重、markdown 回退、主动消息兜底），并会按模式向 system prompt 注入一段 `<qq_response_instruct>`，让模型明确当前契约。

### `direct`（默认）

模型的最终文本原样转发到聊天——送达不依赖模型调用任何东西。需要沉默时，模型输出 `<no_reply/>`，channel 将其吞掉（日志记 `qq.send skip_no_reply`），不发送任何内容。泄漏的模型特殊 token（`<|eos|>`、`<|im_end|>` 等）会从出站文本首尾剥离；输出仅含此类 token 时同样按沉默处理。当所配模型的工具调用可靠性未知时推荐此模式：失败方向是「多发一条」，绝不会「丢一条」。

### `tool`

关闭直通转发（模型输出被路由到 `null` 通道丢弃），改为注册原生 `qq.send` 工具。模型调用 `qq.send` 传入消息文本即回复——`msg_id`/`msg_seq` 在插件内部解析，模型不接触协议字段——不调用即沉默。这与 Bub 原生的 channel 契约一致，且天然支持一轮多条消息。失败方向相反：模型忘记调用工具时回复会静默丢失，请在信任所配模型工具调用能力时使用。

`qq.send` 不受工具策略（`group_tool_policy` / `c2c_tool_policy` / `denied_tools`）限制：它就是回复路径本身，direct 模式下发送回复从来不受门控。

`tool` 模式注意事项：

- 逗号命令的输出在两种模式下都直接送达（命令不经过模型）。
- `llm_rate_limit_notice` 提示文本在 tool 模式下不会送达（被短路的回合产生的是直通输出，tool 模式会丢弃它）；频控本身仍然生效并记录日志。

## 安全

插件为公开聊天场景内置了多层 fail-closed 防护：

1. **白名单** —— 设置 `allow_users` / `allow_groups` 后，名单外的消息在进入模型之前即被丢弃。
2. **逗号命令门控** —— 以 `,` 开头的入站文本只对授权发送者生效：群聊要求平台上报的 `member_role` 为 `owner` / `admin`，或发送者在 `admin_users` 中；私聊仅 `admin_users` 可用。其他人的 `,` 消息按普通文本转发给模型。
3. **工具策略** —— `before_tool_call` hook 按场景拒绝危险工具。群会话默认 `restricted`（禁用 `bash*`、`fs.write`、`fs.edit`、`subagent`）；私聊默认 `open`。第 2 条中的授权发送者不受策略限制。
4. **频控** —— `before_llm_call` hook 按「发送者 + 会话」限制每分钟 LLM 调用次数（`llm_rate_limit_per_minute`），超限时用 `llm_rate_limit_notice` 短路本次调用。
5. **审计日志** —— `after_llm_call` / `after_tool_call` hook 输出 `qq.audit.llm` / `qq.audit.tool` 日志，包含会话、发送者、角色、工具/模型、耗时与错误类型。

注意：不做任何配置时，私聊里的逗号命令不可用（fail-closed）。请把自己的 openid 加入 `admin_users` 以保留命令权限。

### 运维逗号命令

插件内置了对模型不可见（`agent_use=False`）、仅授权发送者可用的逗号命令：

| 命令 | 说明 |
| --- | --- |
| `,qq.version` | 查看已安装的 bub-qq 插件版本 |

其他已注册的 Bub 工具同样可以用 `,名字 参数` 调用；未知的 `,名字` 会兜底为整行 bash 命令执行——因此逗号命令门控实际上等于给授权发送者完整的 shell 权限。

## 运行

QQ 是 channel 监听面。插件安装并配置完成后启动 gateway：

```bash
bub gateway
```

webhook 模式需要将公网 HTTPS 地址指向内嵌服务（上表 host/port/path），并在 QQ 机器人后台登记回调。websocket 模式则需确保后台**没有**被成功配置的 webhook 独占。

`bub chat` 不能替代 QQ 通道收发；QQ 场景请使用 `bub gateway`。

## 会话与消息映射

| 概念 | 格式 / 行为 |
| --- | --- |
| Session ID（C2C） | `qq:c2c:<user_openid>` |
| Chat ID（C2C） | `c2c:<user_openid>` |
| Session ID（群） | `qq:group:<group_openid>` |
| Chat ID（群） | `group:<group_openid>` |
| 入站事件 | `C2C_MESSAGE_CREATE`、`GROUP_AT_MESSAGE_CREATE`、`GROUP_MESSAGE_CREATE` |
| 群聊唤醒 | 收到的群消息一律 `is_active=true`；消息范围由 QQ 客户端群管理员设置 |
| 命令消息 | 以 `,` 开头的入站文本仅对授权发送者转成 Bub `kind=command`（见「安全」），其他人按普通文本处理 |
| 出站 | 文本（`msg_type = 0`），或在回复看起来像 markdown 时发送 markdown（`msg_type = 2`）；**优先被动回复**（`msg_id` + 插件内部管理的 `msg_seq`），群聊可选**主动消息兜底**（`active_messages`，仅纯文本） |
| 被动窗口 | 最近一条入站时间超过 60 分钟后停止被动回复；群聊在开启主动消息时降级为主动发送 |
| 主动授权 | `GROUP_MSG_RECEIVE` / `GROUP_MSG_REJECT`（及 C2C 对应事件）按群/用户持久化；管理员明确拒绝后跳过主动发送 |
| Debounce | `needs_debounce = true` |

C2C 保持仅被动回复：官方文档写明 C2C 主动推送已于 2025-04-21 停止提供。群主动消息需要双侧开启（机器人配置 `active_messages` + 群管理员在 QQ 客户端授权），并消耗平台配额。

## 载荷形状

非命令入站消息会编码为 JSON 字符串，常见字段包括：

- `message`
- `message_id`
- `type`（`text` 或 `attachment`）
- `sender_id`（C2C 为 `user_openid`，群聊为 `member_openid`）
- `sender_name` / `sender_role` / `group_openid` / `chat_type` / `was_mentioned`（群聊）
- `date`
- `attachments`（如有）
- `quoted_messages`（如有：来自 `msg_elements` 的引用消息 / 合并转发聊天记录，含 `message`、可选 `sender_name` 与嵌套 `messages`）

`direct` 模式下普通回复应直接返回最终文本，由 Bub outbound 路由调用 `QQChannel.send`；`tool` 模式下通过 `qq.send` 工具回复。两种模式下 `msg_seq` 都由插件内部管理——不要自行构造协议字段。

## 状态

### 当前支持

- 通过 `qq:` YAML、`BUB_QQ_*` 以及 `bub onboard` 加载配置
- 从 `https://bots.qq.com/app/getAppAccessToken` 获取访问令牌，并按 60 秒窗口缓存刷新
- 基于 `aiohttp` 的 OpenAPI 客户端（`Authorization: QQBot {ACCESS_TOKEN}`）
- 内嵌 webhook 接收、回调验证（`op = 13`）、ed25519 签名流程
- webhook 请求验签（`X-Signature-Ed25519`、`X-Signature-Timestamp`）
- WebSocket 接收路径（重连 / resume，可选分片）
- C2C / 群聊入站适配、`msg_id` 去重、60 分钟被动文本或 markdown 回复
- 群聊文本收发；消息范围由 QQ 客户端群管理员控制
- 同一 `session_id + msg_id + msg_seq` 的内存发送幂等
- OpenAPI 错误暴露（HTTP 状态、平台业务码 `code` / `err_code`、响应头或响应体中的 trace_id）及错误目录元数据
- 多层安全防护：白名单、按角色门控的逗号命令、按场景的工具策略、LLM 频控与审计日志（见「安全」）
- 群主动消息作为被动回复的兜底（`active_messages`），并通过 `*_MSG_RECEIVE` / `*_MSG_REJECT` 事件按群/用户持久化授权状态
- claw_cfg 闭环：`INTERACTION_CREATE` 2002 变更按群持久化，2001 查询回显真实 `require_mention` 状态
- 304023/304024（异步人工审核）按「待审核成功」处理，不再当作发送失败
- 两种回复模式下的选择性沉默：`direct` 模式的 `<no_reply/>` 哨兵过滤、`tool` 模式的「调用即回复、不调用即沉默」，并按模式注入 system prompt 契约说明
- 覆盖配置、鉴权、签名、channel、webhook、websocket、gateway、插件 onboarding、C2C/群聊服务、安全策略、回复模式、平台状态存储等的自动化测试

### 尚未支持

- QQ 频道（Guild）以及富媒体收发
- 除验证、基础 `{"op":12}` 确认、C2C/群消息、消息开关事件、交互查询/变更外的更广 webhook 事件
- C2C 主动推送（平台已于 2025-04-21 停止提供）
- 群主动消息里的 markdown（需报备模板，主动路径仅发纯文本）
- 启动后进程内动态分片再平衡

## 已确认的接口约定

依据 QQ 机器人官方文档（鉴权与事件订阅）：

**鉴权 / OpenAPI**

- 令牌：`POST https://bots.qq.com/app/getAppAccessToken`，请求体 `{ appId, clientSecret }`
- 令牌最长约 `7200` 秒；到期前 `60` 秒内再次请求会返回新令牌，旧令牌在重叠期内仍有效
- OpenAPI 统一请求地址：`https://api.bot.qq.com`（旧地址 `https://api.sgroup.qq.com` 可通过 `openapi_base_url` 覆盖）
- 请求头：`Authorization: QQBot {ACCESS_TOKEN}`
- 失败响应体包含 `err_code`、`message`、`trace_id`（旧格式为 `code`，插件两者都识别）；trace_id 也可从响应头 `X-Tps-trace-ID` 获取

**事件 / 传输**

- 生产环境 webhook 须 HTTPS；端口 `80`、`443`、`8080`、`8443`
- 成功配置有效 HTTPS 回调后，webhook 与 WebSocket 互斥
- 验证请求 `op = 13`；响应须含 `plain_token`，以及对 `event_ts + plain_token` 的 ed25519 签名
- 普通 webhook 验签材料为 `timestamp + raw_body`
- 事件载荷形状：`{ id, op, d, s, t }`
- `C2C_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE` 所属 intent：`GROUP_AND_C2C_EVENT`（`1 << 25`）
- 当前使用的 `C2C_MESSAGE_CREATE.d` 字段：`id`、`author.user_openid`、`content`、`timestamp`、`attachments`
- 当前使用的群事件 `d` 字段：`id`、`group_openid`、`author.member_openid`、`content`、`timestamp`、`mentions`、`attachments`
- 群聊发送：`POST /v2/groups/{group_openid}/messages`，body 与 C2C 相同（`msg_id`、`msg_seq`，以及 `content` + `msg_type = 0` 或 `markdown.content` + `msg_type = 2`）
- WebSocket 关闭码 `4914` / `4915` 视为致命；`4006`–`4009`、`4900`–`4913` 等视为可重连

## 官方文档

- [QQ 机器人开发文档](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/getting-started.html)
- [Bub 文档](https://bub.build/)
- [Bub 插件中心](https://hub.bub.build/)

创建应用、凭据、事件订阅与回调配置（`APPID`、`SECRET`、回调 URL、intents 等）请以 QQ 官方文档为准。

## 开发

```bash
uv run --package bub-qq pytest -q
```

测试使用 mock，不需要连接真实 QQ 网络。

## 许可证

与 [bub-contrib](https://github.com/bubbuild/bub-contrib) 仓库相同。
