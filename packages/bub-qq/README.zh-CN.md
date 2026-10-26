# bub-qq

面向 [Bub](https://bub.build) 的 QQ 开放平台 Channel 适配器。

English documentation: [README.md](./README.md)

## 功能概览

| 能力 | 说明 |
| --- | --- |
| 单聊（C2C）收发 | `C2C_MESSAGE_CREATE` 适配为 Bub `ChannelMessage`；被动文本 / markdown / 富媒体回复 |
| 群聊收发 | `GROUP_AT_MESSAGE_CREATE` / `GROUP_MESSAGE_CREATE`；支持全量消息模式，payload 携带 `was_mentioned` / `sender_role` |
| 群主动消息 | 被动回复不可用时兜底主动发送（`active_messages`，需群管理员在 QQ 客户端授权） |
| 回复模式与选择性沉默 | `reply_mode: tool`（默认）注册原生 `qq.send` 工具，模型调用即回复、不调用即沉默；`direct` 直通转发最终文本、`<no_reply/>` 表示沉默（见「回复模式」） |
| 引用与聊天记录 | 解析 `msg_elements`，将引用消息 / 合并转发内容以 `quoted_messages` 传给模型 |
| 接收模式 | **webhook** 或 **websocket** 二选一（QQ 平台侧互斥）；含 ed25519 验签与断线重连 |
| 安全防护 | 所有操作经过统一检查点 Guard：管理员配对登记（`,qq.claim`）、按资源分类的规则、shell 一次性审批、受保护文件、仅公网下载、白名单、频控、审计日志（见「安全」） |
| 平台状态持久化 | 主动消息授权（`*_MSG_RECEIVE` / `*_MSG_REJECT`）与群 claw_cfg 落盘，重启不丢 |
| 可靠发送 | 入站/出站去重、`msg_seq` 管理、错误码目录；异步人工审核（304023/304024）按「待审核成功」处理 |
| Onboarding | `bub onboard` 交互采集 `appid` / `secret` / `receive_mode`；随包提供 skill 资源（`src/skills/qq`） |

插件入口点：`qq` → `bub_qq.plugin`。当前支持 **单聊（C2C）** 和 **群聊** 文本收发，QQ 频道（Guild）尚未覆盖。

## 前置条件

1. [安装 Bub](https://bub.build/zh-cn/docs/getting-started/install/) 0.6.0 或更新版本（推荐：`uv tool install bub`）。在更旧的 Bub 上插件不会加载，日志会说明原因。更新的小版本和开发版可以加载，但会输出 `qq.bub.untested` / `qq.bub.prerelease` 告警：新版 Bub 增加的工具在 bub-qq 识别之前只对管理员开放
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
| `verify_signature` | `BUB_QQ_VERIFY_SIGNATURE` | `true` | 是否校验 webhook 签名 |
| `webhook_signature_timestamp_tolerance_seconds` | `BUB_QQ_WEBHOOK_SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS` | `300` | webhook 签名时间戳与本地时间的最大允许偏差（秒），用于拦截重放的回调，请保持服务器时间同步；`0` 表示不校验。超过 1 MB 的 webhook 请求体会被拒绝 |
| `inbound_dedupe_size` | `BUB_QQ_INBOUND_DEDUPE_SIZE` | `1024` | 近期入站 `msg_id` 去重缓存大小 |
| `session_state_size` | `BUB_QQ_SESSION_STATE_SIZE` | `1024` | 被动回复会话状态与发送记录的内存条数上限（超出后淘汰最旧条目） |
| `passive_reply_window_seconds` | `BUB_QQ_PASSIVE_REPLY_WINDOW_SECONDS` | 平台限制 | 覆盖入站消息之后尝试被动回复的时间窗口（秒）；不设置时按平台限制：私聊 3600 秒、群聊 300 秒 |
| `active_messages` | `BUB_QQ_ACTIVE_MESSAGES` | `false` | 无法被动回复时改发群主动消息（不带 `msg_id`）；需要群管理员在 QQ 客户端允许机器人主动发言 |
| `passive_replies_per_msg_id` | `BUB_QQ_PASSIVE_REPLIES_PER_MSG_ID` | 平台限制 | 覆盖每条入站消息的被动回复次数上限；不设置时按平台限制：私聊 4 次、群聊 5 次。超出后降级为主动消息（若已开启）或跳过 |
| `group_wake` | `BUB_QQ_GROUP_WAKE` | `all` | 哪些群消息会唤醒模型：`all`（每条都唤醒，由模型决定是否回复）或 `mention`（只有 @ 机器人时唤醒，紧随其后的消息仍会带上）。群消息多时用 `mention` 可节省模型调用 |
| `reply_mode` | `BUB_QQ_REPLY_MODE` | `tool` | 模型输出如何到达 QQ：`tool`（默认）关闭直通转发并注册 `qq.send` 工具；`direct` 直通转发最终文本（输出 `<no_reply/>` 表示沉默）（见「回复模式」） |
| `state_file` | `BUB_QQ_STATE_FILE` | 空 | 持久化已登记管理员与平台开关状态（主动消息授权、群 claw_cfg）的 JSON 文件；留空使用 `<bub home>/qq/state.json` |
| `admin_users` | `BUB_QQ_ADMIN_USERS` | 空 | 管理员，唯一受信任的发送者。条目是带场景的身份（`c2c:<user_openid>`、`group:<group_openid>:<member_openid>`），或在任意场景都匹配的裸 openid。通常留空，用 `,qq.claim` 登记（见「安全」） |
| `allow_users` | `BUB_QQ_ALLOW_USERS` | 空 | 逗号分隔的 C2C 白名单；设置后其他用户的私聊消息会被丢弃 |
| `allow_groups` | `BUB_QQ_ALLOW_GROUPS` | 空 | 逗号分隔的群白名单；设置后其他群的消息会被丢弃 |
| `group_shell` | `BUB_QQ_GROUP_SHELL` | `approval` | 管理员的 shell（`bash`）：`approval`（每条命令都要管理员点击确认，没有「始终允许」）或 `deny`。非管理员永远没有 shell。群聊和私聊都适用 |
| `shell_sandbox` | `BUB_QQ_SHELL_SANDBOX` | `none` | 声明 Bub 是否运行在外部沙箱中（`none` / `external`）。插件本身不隔离命令；开启 shell 且为 `none` 时启动会打印警告 |
| `c2c_access` | `BUB_QQ_C2C_ACCESS` | `admin_users` | 私聊里谁能使用工具：`admin_users`，或 `allow_users`（要求 `allow_users` 非空，否则启动失败）。回复始终可用 |
| `inbound_images` | `BUB_QQ_INBOUND_IMAGES` | `direct` | 哪些收到的图片会附在消息里，让多模态模型直接看到：`direct`（私聊，以及 @ 机器人的群消息）、`all` 或 `off`。其余图片只保留元数据，模型可用 `qq.view_image` 查看 |
| `inbound_images_per_message` | `BUB_QQ_INBOUND_IMAGES_PER_MESSAGE` | `4` | 每条消息最多附加的图片数 |
| `inbound_image_max_bytes` | `BUB_QQ_INBOUND_IMAGE_MAX_BYTES` | `10485760` | 附加或用 `qq.view_image` 查看的单张图片大小上限 |
| `download_allow_hosts` | `BUB_QQ_DOWNLOAD_ALLOW_HOSTS` | 空 | 逗号分隔的主机名；`media_url`、`web.fetch` 和附件下载访问它们时即使解析到内网地址也放行（如内网图床） |
| `group_tool_policy` | `BUB_QQ_GROUP_TOOL_POLICY` | `restricted` | 群聊中 shell 和文件以外其他工具的策略：`open` / `restricted`（禁用 `subagent` 和 `denied_tools`）/ `locked`（除回复外禁用全部工具，管理员也一样） |
| `c2c_tool_policy` | `BUB_QQ_C2C_TOOL_POLICY` | `open` | 同上，作用于 `c2c_access` 允许的私聊用户 |
| `allowed_tools` | `BUB_QQ_ALLOWED_TOOLS` | 空 | 允许非管理员使用的额外工具名 glob，逗号分隔，如 `web_search.*`。内置清单和这里都没有的工具只给管理员用；shell、code mode 和改动会话的工具不受影响 |
| `denied_tools` | `BUB_QQ_DENIED_TOOLS` | 空 | `restricted` 策略下额外禁用的工具名 glob，逗号分隔，如 `web.fetch,tape.*` |
| `llm_rate_limit_per_minute` | `BUB_QQ_LLM_RATE_LIMIT_PER_MINUTE` | `0` | 每发送者在每个会话内每分钟的 LLM 调用上限；`0` 表示不限 |
| `llm_rate_limit_notice` | `BUB_QQ_LLM_RATE_LIMIT_NOTICE` | `请求过于频繁，请稍后再试。` | 触发频控时回复的文本 |
| `websocket_intents` | `BUB_QQ_WEBSOCKET_INTENTS` | `1 << 25` | WebSocket identify intents（`GROUP_AND_C2C_EVENT`） |
| `websocket_use_shard_gateway` | `BUB_QQ_WEBSOCKET_USE_SHARD_GATEWAY` | `false` | 是否按 `/gateway/bot` 建议分片数连接 |
| `websocket_reconnect_delay_seconds` | `BUB_QQ_WEBSOCKET_RECONNECT_DELAY_SECONDS` | `5` | WebSocket 重连基础延迟（连续失败时指数递增） |
| `websocket_reconnect_max_delay_seconds` | `BUB_QQ_WEBSOCKET_RECONNECT_MAX_DELAY_SECONDS` | `300` | 指数退避的延迟上限 |
| `websocket_max_identify_rejections` | `BUB_QQ_WEBSOCKET_MAX_IDENTIFY_REJECTIONS` | `5` | 连续 identify 被拒（op 9）达到该次数后停止客户端；`0` 表示不限制 |

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

0.3.0 移除了 `exec_approval`（改用 `group_shell`）和 `workspace_jail`（由 Guard 取代，见「安全」）。如果仍然设置，启动时会打印警告并忽略。

群聊能听到哪些消息，由 QQ 客户端里群管理员的「允许机器人可获取的群聊消息范围」决定（全部消息 / @ 最近 10 条 / 仅 @）。`group_wake: all`（默认）时插件对收到的每条群消息都会唤醒模型，未 @ 时 payload 里 `was_mentioned` 为 `false`，模型可按当前回复模式选择不回复；`group_wake: mention` 时只有 @ 机器人才会唤醒。

最新版手机 QQ 中的设置路径：**进入群聊 → 右上角「更多」 → 群机器人 → 管理**。群主或管理员可在此调整「机器人可获取的群聊消息范围」，以及是否「允许机器人主动发言」（配合 `active_messages` 使用）。

## 回复模式

`reply_mode` 决定模型输出如何变成 QQ 消息，以及同样重要的——模型如何保持沉默（例如群聊中未被 @ 且无话可说时）。用 opt-in/opt-out 的语言说：`direct` 是 **opt-out**（默认回复，模型输出哨兵显式退出本轮）；`tool` 是 **opt-in**（默认沉默，模型调用发送工具显式选择回复——与 Bub 其他通道的契约一致）。两种模式共享同一条发送链路（被动 `msg_id`/`msg_seq` 定位、去重、markdown 回退、主动消息兜底），并会按模式向 system prompt 注入一段 `<qq_response_instruct>`，让模型明确当前契约。

### `tool`（默认）

关闭直通转发（模型输出被路由到 `null` 通道丢弃），改为注册原生 `qq.send` 工具。模型调用 `qq.send` 传入消息文本即回复（可选 `media_url` / `file_type` 发原生富媒体）——`msg_id`/`msg_seq` 在插件内部解析，模型不接触协议字段——不调用即沉默。按钮不由模型生成。失败方向：模型忘记调用工具时回复会静默丢失。

### `direct`

模型的最终文本原样转发到聊天——送达不依赖模型调用任何东西。需要沉默时，模型输出 `<no_reply/>`，channel 将其吞掉（日志记 `qq.send skip_no_reply`），不发送任何内容。泄漏的模型特殊 token（`<|eos|>`、`<|im_end|>` 等）会从出站文本首尾剥离；输出仅含此类 token 时同样按沉默处理。当所配模型的工具调用可靠性未知时可改回此模式：失败方向是「多发一条」，绝不会「丢一条」。`direct` 下不能发 `media_url`。

`qq.send` 不受工具策略（`group_tool_policy` / `c2c_tool_policy` / `denied_tools`）限制：它就是回复路径本身，direct 模式下发送回复从来不受门控。

`tool` 模式注意事项：

- 逗号命令的输出在两种模式下都直接送达（命令不经过模型）。
- 发送者触发 `llm_rate_limit_per_minute` 时，插件会自己发送 `llm_rate_limit_notice`（每个发送者每分钟最多一次）。频控按回合计数，多步工具调用的一个回合只算一次。
- `qq.send` 回复的是触发本回合的那条消息，即使期间又有新消息进来；按钮点击按 QQ 的要求通过 `event_id` 回复。

## 安全

群聊能触发的每一个操作都经过同一个检查点 **Guard**：模型的工具调用和逗号命令（都在 `before_tool_call` 中检查，Bub 执行命令时同样经过工具 hook），以及审批通过后的执行。它只根据「谁在请求」和「调用要碰哪类资源」做决定。任何能给机器人发消息的人都能影响模型，所以模型发起的工具调用按发送者的身份对待。

**谁可信。** 只有管理员：`admin_users` 里的身份，加上用 `,qq.claim` 登记的身份。Bub 会把短时间内连续到达的消息合并成一轮处理；这一轮只有在所有发送者都是管理员时才按管理员对待，避免普通成员的请求借紧随其后的管理员消息获得权限。不带 QQ 发送者信息进入 QQ 会话的对话（定时任务、其他插件触发）按身份未知的成员检查，在私聊中也不会被当作管理员（日志：`qq.security.unattributed_turn`）。QQ 群主、群管理员的角色不带来任何权限，因为部署机器人的人不一定是群主。同一个人在私聊和每个群里的 openid 都不同，所以身份按场景区分（`c2c:<user_openid>`、`group:<group_openid>:<member_openid>`）。

**各类资源的规则**（默认配置）：

| 资源 | 其他成员 | 管理员 |
| --- | --- | --- |
| shell（`bash`） | 拒绝 | 每条命令都要审批（`group_shell`） |
| 读文件（`fs.read`） | 工作区内 | 工作区内 |
| 写文件（`fs.write` / `fs.edit`） | 拒绝 | 工作区内 |
| 受保护文件（`.env*`、`.git/`、状态文件） | 拒绝 | 拒绝 |
| 发送文件（`qq.send media_path`） | 仅 `outbox/`、`inbox/` | 同左 |
| 下载（`media_url`、`web.fetch`、附件） | 仅公网地址，每一跳重定向都检查；附件另外放行 QQ 媒体域名（`qq.com.cn`、`qpic.cn`、`qq.com`），其网址由 QQ 提供 | 同左 |
| code mode（`run_code` / `code_mode`） | 拒绝 | 拒绝 |
| 只读工具（`tape.info` / `tape.search` / `tape.anchors` / `tape.handoff`、`skill`、`tool.describe`、`spill.read`、`help`、`qq.fetch_attachment`、`qq.view_image`） | `group_tool_policy` / `c2c_tool_policy` | 同左 |
| 改动会话的工具（`tape.reset`、`subagent`、`model`、`reasoning_effort`、`quit`） | 拒绝 | `group_tool_policy` / `c2c_tool_policy` |
| 未列出的工具（其他插件、MCP、Bub 新增的工具） | 拒绝，除非匹配 `allowed_tools` | `group_tool_policy` / `c2c_tool_policy` |

私聊中默认只有管理员能用工具（`c2c_access`），回复始终可用。

**审批。** 管理员的 shell 命令会发出一张卡片，完整显示命令（太长无法完整显示的命令直接拒绝，不会截断），附两个按钮，只有该群已登记的管理员能点。点「允许」后签发一次性凭证，绑定会话、请求人、工具和完整参数，240 秒内有效；命令随后带着凭证重新经过 Guard。卡片直接显示命令本身（其他参数写在 `#` 开头的一行里），并对 `rm -rf`、`find -delete`、`dd of=`、`curl | sh` 等高危模式加上警告，并对下载网络内容的命令（带网址的 `curl`/`wget`/`aria2c`，不经过插件的地址检查）加上注意提示；两者都只是提示，是否批准仍由管理员决定。没有「始终允许」。无论是模型发起的调用还是管理员的逗号命令，批准后都作为逗号命令（模型调用会先改写成等价的 `,bash command=…`）交回 Bub，在原会话的执行环境里运行，结果发到聊天里并写入会话 tape。Bub 不会把命令输出放进模型上下文，因此每个审批的结局（`ran` 及输出、`denied`、`expired`、`refused`、`failed`）会在该会话下一条消息中以 `approval_results` 附加一次（最近 3 条，输出截断到 500 字符）。

**插件不对 shell 做隔离。** bub-qq 不限制审批通过的命令能做什么。请把 Bub 运行在容器等沙箱里并设置 `shell_sandbox=external`，或者设置 `group_shell=deny`。

**成为管理员。** 首次启动且没有管理员时，日志会打印一次性配对码：

```
qq.admin.unclaimed no admins configured; send ',qq.claim K7QX2M9PHT' to the bot in a private (C2C) chat within 10 minutes to become its admin
```

1. 加机器人为好友，在私聊中发送 `,qq.claim <配对码>`。在群里发送的配对码会作废并换新。
2. 要在某个群里以管理员身份操作，先在私聊中发送 `,qq.claim group`，再把返回的群登记码（1 分钟有效、只能用一次）发到那个群。登记码被使用后，申请人会收到私聊通知，写明登记的是哪个身份；如果码被别人抢先用掉，可以用 `,qq.admins remove` 撤销。
3. `,qq.admins` 列出管理员；`,qq.admins remove <身份>` 移除已登记的管理员。

同一发送者连续输错 5 次会被锁定 10 分钟；机器人只提示一次，锁定期间不再回复这个人的认领消息，避免被人利用来刷屏。认领和管理命令由插件直接处理，不会进入模型上下文和对话记录。也可以跳过配对，直接把身份写进 `admin_users`。

另外还有：白名单（`allow_users` / `allow_groups`）在进入模型之前就丢弃名单外的消息；`llm_rate_limit_per_minute` 限制每个发送者的 LLM 调用频率；`qq.audit.llm` / `qq.audit.tool` 日志记录每一次调用（`qq.audit.llm` 的 `requested_model` 是请求的模型；配置了 `fallback_models` 时实际回答的模型请看 trace 中的 `chat` span）。

### 运维逗号命令

命令前缀跟随 Bub 的 `BUB_COMMAND_PREFIX`（默认 `,`，下文以 `,` 为例）。逗号命令只接受管理员发送；其他人的 `,` 消息作为普通文本交给模型。每条命令都按 Bub 实际执行的方式由 Guard 检查。运维命令以 `exposure="command"` 注册，不会出现在模型的工具列表中。

| 命令 | 说明 |
| --- | --- |
| `,qq.claim <配对码>` / `,qq.claim group` | 登记为管理员（见上文）；任何人都可以发送 |
| `,qq.admins` / `,qq.admins remove <身份>` | 列出或移除管理员 |
| `,qq.version` | 查看已安装的 bub-qq 插件版本 |

其他已注册的 Bub 工具可以用 `,名字 参数` 调用；未知的 `,名字` 会被 Bub 当作整行 shell 命令执行，因此适用 shell 的规则。

## 运行

QQ 是 channel 监听面。插件安装并配置完成后启动 gateway：

```bash
bub gateway
```

工作区就是启动时的 pwd。建议先准备一个专用目录，再在里面启动，这样下载的附件会落在该目录的 `inbox/` 下，插件下载的文件落在 `outbox/` 下：

```bash
mkdir -p ~/Documents/bub-qq-work
cd ~/Documents/bub-qq-work
bub gateway
```

如果从 `/` 或家目录启动（或当前目录不可写），这两个目录会改到 `~/.bub/qq/` 下，避免在系统根目录散落文件。文件工具仍然只能在启动时的 pwd 内操作。

webhook 模式需要将公网 HTTPS 地址指向内嵌服务（上表 host/port/path），并在 QQ 机器人后台登记回调。websocket 模式则需确保后台**没有**被成功配置的 webhook 独占。

`bub chat` 不能替代 QQ 通道收发；QQ 场景请使用 `bub gateway`。

想看 QQ 发来的原始事件（比如确认引用或转发的聊天记录里带了什么），把 Bub 的 `verbose` 设为 `1`（或设置环境变量 `BUB_VERBOSE=1`）：每个事件都会以 `qq.transport.payload` 的 DEBUG 日志打印在 gateway 的 stderr 上。Bub 不写日志文件，需要保存时用 `bub gateway 2>&1 | tee gateway.log`。这些日志包含消息原文、openid 和带临时下载凭证的附件网址；排查完请把 `verbose` 改回去，日志也不要原样外发。

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
| 出站 | 默认 `qq.send`：文本 / markdown（`msg_type = 0/2`）、富媒体（`msg_type = 7`；插件先下载 `media_url` 再分片上传，或 workspace 本地 `media_path`）；**优先被动回复**（`msg_id` + 插件内部管理的 `msg_seq`），群聊可选**主动消息兜底**（`active_messages`，仅纯文本；富媒体不走主动路径） |
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
- `quoted_messages`（如有：来自 `msg_elements` 的引用消息 / 合并转发聊天记录，含 `message`、可选 `sender_name`、`attachments` 与嵌套 `messages`；引用里的附件带 `index`，编号接在消息自身附件之后）
- `message_type`（0=文本，3=结构化卡片，101/102/103=引用或聊天记录）
- `ark_data`（`message_type=3` 时的卡片数据）

发给机器人的消息里的图片（PNG、JPEG、GIF、WebP）会附在消息中，多模态模型可以直接看到；这些图片在 `attachments` 中标记为 `"attached": true`，在 Bub 组装 prompt 时才下载（仅限 QQ 媒体域名或公网地址，有大小上限）。其余图片只保留元数据，模型可用 `qq.view_image(message_id, index)` 查看，该工具直接返回图片本身。其他文件不会在收到时下载，`qq.fetch_attachment(message_id, index)` 会把文件保存到 `inbox/<message_id>/`。两个工具都只能访问同一会话中的消息。引用消息里的附件通过引用它的那条消息访问，序号就是 `quoted_messages` 中的 `index`，自动附图规则与消息自身的图片相同；QQ 会为引用里的附件重新签发下载网址，所以引用一张旧图，即使原网址已过期或 gateway 重启过，机器人也能重新下载。地址被拒绝时，返回给模型的提示为 `Private network address check failed, access refused.`，并附一句说明，要求模型不要换其他方式（bash、curl、脚本）下载。附件下载信任 QQ 媒体域名，所以在 Surge、Clash 等 fake-IP 代理下也能正常使用。附加的图片会留在会话历史中直到 handoff，所以繁忙的群里请保持 `inbound_images: direct`。

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
- `qq.send` 富媒体：下载 `media_url`（仅公网地址）或使用 `media_path`（仅 `outbox/`、`inbox/`），再分片上传并以 `msg_type = 7` 发送（单聊/群聊隔离；仅被动窗口）
- 为多模态模型提供收到的图片（直接发给机器人的消息自动附加，其余用 `qq.view_image` 查看），以及用 `qq.fetch_attachment` 按需下载文件
- 插件自有消息按钮（固定模板）；`INTERACTION_CREATE` type 11/12 先 PUT 再入站
- 群聊文本收发；消息范围由 QQ 客户端群管理员控制
- 同一 `session_id + msg_id + msg_seq` 的内存发送幂等
- OpenAPI 错误暴露（HTTP 状态、平台业务码 `code` / `err_code`、响应头或响应体中的 trace_id）及错误目录元数据
- 基于 Guard 的安全防护：管理员配对登记、按资源分类的规则、shell 一次性审批、受保护文件、仅公网下载、白名单、LLM 频控与审计日志（见「安全」）
- 群主动消息作为被动回复的兜底（`active_messages`），并通过 `*_MSG_RECEIVE` / `*_MSG_REJECT` 事件按群/用户持久化授权状态
- claw_cfg 闭环：`INTERACTION_CREATE` 2002 变更按群持久化，2001 查询回显真实 `require_mention` 状态
- 304023/304024（异步人工审核）按「待审核成功」处理，不再当作发送失败
- 两种回复模式下的选择性沉默：`direct` 模式的 `<no_reply/>` 哨兵过滤、`tool` 模式的「调用即回复、不调用即沉默」，并按模式注入 system prompt 契约说明
- 覆盖配置、鉴权、签名、channel、webhook、websocket、gateway、插件 onboarding、C2C/群聊服务、安全策略、回复模式、平台状态存储等的自动化测试

### 尚未支持

- QQ 频道（Guild）
- shell 命令沙箱（可用 `shell_sandbox` 声明外部沙箱）
- 除验证、基础 `{"op":12}` 确认、C2C/群消息、消息开关事件、交互查询/变更、按钮/菜单点击外的更广 webhook 事件
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
- 群聊发送：`POST /v2/groups/{group_openid}/messages`，body 与 C2C 相同（`msg_id`、`msg_seq`，以及 `content` + `msg_type = 0`、`markdown.content` + `msg_type = 2`，或 `media.file_info` + `msg_type = 7`）
- 富媒体上传：本机分片（`upload_prepare` / `upload_part_finish` / `files` 用 `upload_id` 合并）再 `msg_type = 7`；单聊与群聊文件不能混用
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
