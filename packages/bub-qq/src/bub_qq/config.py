from __future__ import annotations

from typing import Literal

import bub
from pydantic import Field
from pydantic import field_validator
from pydantic_settings import SettingsConfigDict

type ToolPolicy = Literal["open", "restricted", "locked"]
type ReplyMode = Literal["direct", "tool"]
type ReceiveMode = Literal["webhook", "websocket"]
type GroupWake = Literal["all", "mention"]
type GroupShell = Literal["deny", "approval"]
type ShellSandbox = Literal["none", "external"]
type C2CAccess = Literal["admin_users", "allow_users"]
type InboundImages = Literal["direct", "all", "off"]


@bub.config(name="qq")
class QQConfig(bub.Settings):
    """QQ Open Platform adapter config."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_QQ_",
        env_file=".env",
        extra="ignore",
    )

    appid: str = ""
    secret: str = ""
    token_url: str = "https://bots.qq.com/app/getAppAccessToken"
    openapi_base_url: str = "https://api.bot.qq.com"
    timeout_seconds: float = 30.0
    token_refresh_skew_seconds: int = 60
    receive_mode: ReceiveMode | Literal[""] = Field(
        default="",
        description="QQ inbound transport mode. Must be set to 'webhook' or 'websocket' before gateway start.",
    )
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    webhook_path: str = "/qq/webhook"
    verify_signature: bool = True
    webhook_signature_timestamp_tolerance_seconds: float = Field(
        default=300.0,
        ge=0,
        description=(
            "Reject webhook requests whose signature timestamp deviates from local"
            " time by more than this many seconds. 0 disables the freshness check."
        ),
    )
    inbound_dedupe_size: int = Field(default=1024, ge=1)
    session_state_size: int = Field(
        default=1024,
        ge=1,
        description="Max sessions/send records kept in memory for passive replies.",
    )
    passive_reply_window_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Override how long after an inbound message passive replies are"
            " attempted. Unset uses the platform limits: 3600 s in C2C,"
            " 300 s in groups."
        ),
    )
    active_messages: bool = Field(
        default=False,
        description=(
            "Send proactive group messages (no msg_id) when a passive reply"
            " is impossible. Requires the group admin to allow proactive"
            " messages in the QQ client; consumes platform quota."
        ),
    )
    reply_mode: ReplyMode = Field(
        default="tool",
        description=(
            "How model output reaches QQ. 'tool' (default) disables direct"
            " forwarding and exposes the qq.send tool; staying silent means"
            " not calling it. 'direct' forwards the model's final text as"
            " the reply (output exactly <no_reply/> to stay silent)."
        ),
    )
    passive_replies_per_msg_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Override the local cap of passive replies per inbound msg_id;"
            " beyond it the send falls back to an active message (when"
            " enabled) or is skipped. Unset uses the platform limits: 4 in"
            " C2C, 5 in groups."
        ),
    )
    state_file: str = Field(
        default="",
        description=(
            "Path of the JSON file persisting platform switches (active"
            "-message opt-ins, group claw_cfg). Empty uses"
            " <bub home>/qq/state.json."
        ),
    )
    websocket_intents: int = 1 << 25
    websocket_use_shard_gateway: bool = False
    websocket_reconnect_delay_seconds: float = 5.0
    websocket_reconnect_max_delay_seconds: float = 300.0
    websocket_max_identify_rejections: int = 5
    admin_users: str = Field(
        default="",
        description=(
            "Comma-separated admins, the only trusted senders. Entries are"
            " scoped identities ('c2c:<user_openid>',"
            " 'group:<group_openid>:<member_openid>') or bare openids that"
            " match in any scope. QQ group owners/admins are not trusted"
            " unless listed here or registered with ,qq.claim. Comma commands"
            " from anyone else are treated as plain text."
        ),
    )
    allow_users: str = Field(
        default="",
        description=(
            "Comma-separated C2C user openid allowlist. When set, C2C"
            " messages from anyone else are dropped. Empty allows everyone."
        ),
    )
    allow_groups: str = Field(
        default="",
        description=(
            "Comma-separated group openid allowlist. When set, messages from"
            " other groups are dropped. Empty allows every group."
        ),
    )
    group_wake: GroupWake = Field(
        default="all",
        description=(
            "Which group messages start a model turn: 'all' (every message"
            " QQ delivers; the model decides whether to reply) or 'mention'"
            " (only @-mentions; follow-ups right after are still included)."
            " Saves model calls in busy groups."
        ),
    )
    group_shell: GroupShell = Field(
        default="approval",
        description=(
            "Shell (bash) access for admins: 'approval' asks an admin to"
            " confirm every command on a keyboard card (no always-allow),"
            " 'deny' refuses shell entirely. Non-admins never get a shell."
            " Applies in groups and in C2C."
        ),
    )
    shell_sandbox: ShellSandbox = Field(
        default="none",
        description=(
            "Declare whether bash runs inside an external sandbox (container,"
            " bwrap, ...). The plugin cannot verify this and does not isolate"
            " commands itself; 'none' logs a warning when shell is enabled."
        ),
    )
    c2c_access: C2CAccess = Field(
        default="admin_users",
        description=(
            "Who may use tools in C2C: 'admin_users' (admins only) or"
            " 'allow_users' (everyone in the non-empty allow_users list)."
            " Replies via qq.send always work."
        ),
    )
    inbound_images: InboundImages = Field(
        default="direct",
        description=(
            "Which inbound images are attached to the message for the model to"
            " see: 'direct' (private chats and group messages that @ the bot),"
            " 'all', or 'off'. Others stay metadata; the model opens them with"
            " qq.view_image."
        ),
    )
    inbound_images_per_message: int = Field(
        default=4, ge=0, description="Most images attached to one inbound message."
    )
    inbound_image_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        description="Largest image attached or opened with qq.view_image, in bytes.",
    )
    download_allow_hosts: str = Field(
        default="",
        description=(
            "Comma-separated hostnames that qq.send media_url, web.fetch and"
            " attachment downloads may reach even when they resolve to a private"
            " address (e.g. an intranet image host). Every other host must be a"
            " public internet address; attachments may also come from QQ's media"
            " domains."
        ),
    )
    exec_approval: bool | None = Field(
        default=None,
        description="Removed in 0.3.0; replaced by group_shell. Ignored with a warning.",
    )
    workspace_jail: bool | None = Field(
        default=None,
        description=(
            "Removed in 0.3.0; replaced by the Guard allowlists and"
            " shell_sandbox. Ignored with a warning."
        ),
    )
    group_tool_policy: ToolPolicy = Field(
        default="restricted",
        description=(
            "Policy for tools other than shell and files in group sessions:"
            " 'open' allows them, 'restricted' denies subagent and"
            " denied_tools, 'locked' denies all of them. Applies to admins"
            " too; shell and file access follow group_shell and the Guard."
        ),
    )
    c2c_tool_policy: ToolPolicy = Field(
        default="open",
        description="Tool policy for C2C sessions; same values as group_tool_policy.",
    )
    allowed_tools: str = Field(
        default="",
        description=(
            "Extra comma-separated tool-name glob patterns non-admins may use,"
            " e.g. 'web_search.*'. Tools outside the built-in allowlist and"
            " these patterns are admin-only."
        ),
    )
    denied_tools: str = Field(
        default="",
        description=(
            "Extra comma-separated tool-name glob patterns denied under the"
            " 'restricted' policy, e.g. 'web.fetch,tape.*'."
        ),
    )
    llm_rate_limit_per_minute: int = Field(
        default=0,
        ge=0,
        description=(
            "Max LLM calls per sender per session per minute; exceeding calls"
            " are short-circuited with llm_rate_limit_notice. 0 disables."
        ),
    )
    llm_rate_limit_notice: str = Field(
        default="请求过于频繁，请稍后再试。",
        description="Reply text used when a sender hits the LLM rate limit.",
    )

    @field_validator("receive_mode", mode="before")
    @classmethod
    def _normalize_receive_mode(cls, value: object) -> object:
        # Accept "WebSocket", " webhook " etc. as before the Literal type.
        return value.strip().lower() if isinstance(value, str) else value
