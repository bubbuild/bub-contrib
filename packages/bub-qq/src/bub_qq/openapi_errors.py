from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


def _e(
    code: int,
    name: str,
    description: str,
    category: str,
    retryable: bool,
) -> "QQKnownOpenAPIError":
    return QQKnownOpenAPIError(
        code=code,
        name=name,
        description=description,
        category=category,
        retryable=retryable,
    )


@dataclass(frozen=True)
class QQKnownOpenAPIError:
    code: int
    name: str
    description: str
    category: str
    retryable: bool


@dataclass
class QQOpenAPIError(RuntimeError):
    """QQ OpenAPI failure with transport, trace, and known-code context."""

    status_code: int
    trace_id: str | None
    error_code: int | None
    error_message: str
    response_body: Any | None = None
    known: QQKnownOpenAPIError | None = None

    def __str__(self) -> str:
        parts = [f"http={self.status_code}"]
        if self.error_code is not None:
            parts.append(f"code={self.error_code}")
        if self.trace_id:
            parts.append(f"trace_id={self.trace_id}")
        if self.known is not None:
            parts.append(f"category={self.known.category}")
            parts.append(f"retryable={str(self.known.retryable).lower()}")
            parts.append(self.known.name)
        parts.append(self.error_message)
        return "qq openapi error: " + " ".join(parts)


HTTP_STATUS_DESCRIPTIONS: dict[int, str] = {
    200: "成功",
    201: "异步操作成功，但会返回错误体，需要特殊处理",
    202: "异步操作成功，但会返回错误体，需要特殊处理",
    204: "成功，无包体",
    401: "认证失败",
    404: "未找到 API",
    405: "HTTP method 不允许",
    429: "频率限制",
    500: "处理失败",
    504: "处理失败",
}


KNOWN_OPENAPI_ERRORS: dict[int, QQKnownOpenAPIError] = {
    10001: _e(10001, "UnknownAccount", "账号异常", "auth", False),
    10003: _e(10003, "UnknownChannel", "子频道异常", "resource", False),
    10004: _e(10004, "UnknownGuild", "频道异常", "resource", False),
    11241: _e(11241, "ErrorWrongToken", "参数中缺少 token", "auth", False),
    11242: _e(11242, "ErrorCheckTokenFailed", "校验 token 失败", "auth", True),
    11243: _e(11243, "ErrorCheckTokenNotPass", "用户填充的 token 错误", "auth", False),
    11251: _e(11251, "ErrorWrongAppid", "appid 错误", "auth", False),
    11252: _e(11252, "ErrorCheckAppPrivilegeFailed", "检查应用权限失败", "permission", True),
    11253: _e(11253, "ErrorCheckAppPrivilegeNotPass", "应用未获得调用接口权限", "permission", False),
    11254: _e(11254, "ErrorInterfaceForbidden", "接口被封禁", "permission", False),
    11261: _e(11261, "ErrorWrongAppid", "参数中缺少 appid", "auth", False),
    11262: _e(11262, "ErrorCheckRobot", "当前接口不支持使用机器人 Bot Token 调用", "auth", False),
    11263: _e(11263, "ErrorCheckGuildAuth", "检查频道权限失败", "permission", True),
    11264: _e(11264, "ErrorGuildAuthNotPass", "频道权限未通过", "permission", False),
    11265: _e(11265, "ErrorRobotHasBaned", "机器人已经被封禁", "auth", False),
    11273: _e(11273, "ErrorCheckUserAuth", "当前接口不支持使用 Bearer Token 调用", "auth", False),
    11274: _e(11274, "ErrorUserAuthNotPass", "用户 OAuth 授权权限未通过", "permission", False),
    11275: _e(11275, "ErrorWrongAppid", "无 appid", "auth", False),
    11281: _e(11281, "ErrorCheckAdminFailed", "检查是否是管理员失败", "permission", True),
    11282: _e(11282, "ErrorCheckAdminNotPass", "管理员权限未通过", "permission", False),
    11301: _e(11301, "ErrorGetHTTPHeader", "HTTP Header 无效", "request", False),
    11302: _e(11302, "ErrorGetHeaderUIN", "HTTP Header 无效", "request", False),
    11303: _e(11303, "ErrorGetNick", "获取昵称失败", "platform", True),
    11304: _e(11304, "ErrorGetAvatar", "获取头像失败", "platform", True),
    11305: _e(11305, "ErrorGetGuildID", "获取频道 ID 失败", "platform", True),
    11306: _e(11306, "ErrorGetGuildInfo", "获取频道信息失败", "platform", True),
    12001: _e(12001, "ReplaceIDFailed", "替换 id 失败", "request", False),
    12002: _e(12002, "RequestInvalid", "请求体错误", "request", False),
    12003: _e(12003, "ResponseInvalid", "回包错误", "platform", True),
    20028: _e(20028, "ChannelHitWriteRateLimit", "子频道消息触发限频", "rate_limit", True),
    22009: _e(22009, "MsgLimitExceed", "消息发送超频", "rate_limit", True),
    50006: _e(50006, "CannotSendEmptyMessage", "消息为空", "request", False),
    50035: _e(50035, "InvalidFormBody", "form-data 内容异常", "request", False),
    50037: _e(50037, "MarkdownKeyboardOnly", "带有 markdown 消息只支持 markdown 或 keyboard 组合", "request", False),
    50038: _e(50038, "CrossChannelReference", "非同频道同子频道", "request", False),
    50039: _e(50039, "GetMessageFailed", "获取消息失败", "platform", True),
    50040: _e(50040, "MarkdownTemplateTypeError", "消息模版类型错误", "request", False),
    50041: _e(50041, "MarkdownEmptyValue", "markdown 有空值", "request", False),
    50042: _e(50042, "MarkdownListTooLong", "markdown 列表超限", "request", False),
    50043: _e(50043, "GuildIdConvertFailed", "guild_id 转换失败", "request", False),
    50045: _e(50045, "CannotReplySelf", "不能回复机器人自己产生的消息", "reply", False),
    50046: _e(50046, "NotAtBotMessage", "非 at 机器人消息", "reply", False),
    50047: _e(50047, "NotBotOrAtBotMessage", "非机器人产生的消息或者 at 机器人消息", "reply", False),
    50048: _e(50048, "MessageIdRequired", "message id 不能为空", "reply", False),
    50049: _e(50049, "KeyboardOnlyEditable", "只能修改含有 keyboard 元素的消息", "edit_message", False),
    50050: _e(50050, "KeyboardRequiredWhenEditing", "修改消息时 keyboard 元素不能为空", "edit_message", False),
    50051: _e(50051, "OnlyOwnMessageEditable", "只能修改机器人自己发送的消息", "edit_message", False),
    50053: _e(50053, "EditMessageFailed", "修改消息错误", "edit_message", True),
    50054: _e(50054, "MarkdownTemplateParamError", "markdown 模版参数错误", "request", False),
    50055: _e(50055, "InvalidMarkdownContent", "无效的 markdown content", "request", False),
    50056: _e(50056, "MarkdownContentForbidden", "不允许发送 markdown content", "permission", False),
    50057: _e(50057, "MarkdownModeConflict", "markdown 参数只支持原生语法或者模版二选一", "request", False),
    301000: _e(301000, "ChannelPermissionParamInvalid", "参数错误", "channel_permission", False),
    301001: _e(301001, "ChannelPermissionQueryGuildError", "查询频道信息错误", "channel_permission", True),
    301002: _e(301002, "ChannelPermissionQueryError", "查询子频道权限错误", "channel_permission", True),
    301003: _e(301003, "ChannelPermissionUpdateError", "修改子频道权限错误", "channel_permission", True),
    301004: _e(301004, "PrivateChannelMemberLimit", "私密子频道关联人数到达上限", "channel_permission", False),
    301005: _e(301005, "ChannelPermissionRPCFailed", "调用 Rpc 服务失败", "channel_permission", True),
    301006: _e(301006, "NonMemberNoPermission", "非群成员没有查询权限", "channel_permission", False),
    301007: _e(301007, "ParamOverLimit", "参数超过数量限制", "channel_permission", False),
    302000: _e(302000, "ScheduleParamInvalid", "参数错误", "schedule", False),
    302001: _e(302001, "ScheduleQueryGuildError", "查询频道信息错误", "schedule", True),
    302002: _e(302002, "ScheduleListFailed", "查询日程列表失败", "schedule", True),
    302003: _e(302003, "ScheduleGetFailed", "查询日程失败", "schedule", True),
    302004: _e(302004, "SchedulePatchFailed", "修改日程失败", "schedule", True),
    302005: _e(302005, "ScheduleDeleteFailed", "删除日程失败", "schedule", True),
    302006: _e(302006, "ScheduleCreateFailed", "创建日程失败", "schedule", True),
    302007: _e(302007, "ScheduleCreatorInfoFailed", "获取创建者信息失败", "schedule", True),
    302008: _e(302008, "ChannelIdRequired", "子频道 ID 不能为空", "schedule", False),
    302009: _e(302009, "ScheduleSystemError", "频道系统错误", "schedule", True),
    302010: _e(302010, "SchedulePermissionDenied", "暂无修改日程权限", "schedule", False),
    302011: _e(302011, "ScheduleDeleted", "日程活动已被删除", "schedule", False),
    302012: _e(302012, "ScheduleDailyLimit", "每天只能创建 10 个日程", "schedule", False),
    302013: _e(302013, "ScheduleSafetyBlocked", "创建日程触发安全打击", "schedule", False),
    302014: _e(302014, "ScheduleTooLong", "日程持续时间超过 7 天", "schedule", False),
    302015: _e(302015, "ScheduleStartTooEarly", "开始时间不能早于当前时间", "schedule", False),
    302016: _e(302016, "ScheduleEndTooEarly", "结束时间不能早于开始时间", "schedule", False),
    302017: _e(302017, "ScheduleObjectEmpty", "Schedule 对象为空", "schedule", False),
    302018: _e(302018, "ScheduleTypeConvertFailed", "参数类型转换失败", "schedule", False),
    302019: _e(302019, "ScheduleDownstreamFailed", "调用下游失败", "schedule", True),
    302020: _e(302020, "ScheduleContentViolation", "日程内容违规、账号违规", "schedule", False),
    302021: _e(302021, "ScheduleDailyGuildLimit", "频道内当日新增活动达上限", "schedule", False),
    302022: _e(302022, "ScheduleBindWrongChannel", "不能绑定非当前频道的子频道", "schedule", False),
    302023: _e(302023, "ScheduleBindForbiddenOnJump", "开始时跳转不可绑定日程子频道", "schedule", False),
    302024: _e(302024, "ScheduleBoundChannelMissing", "绑定的子频道不存在", "schedule", False),
    304003: _e(304003, "URL_NOT_ALLOWED", "URL 未报备", "content", False),
    304004: _e(304004, "ARK_NOT_ALLOWED", "没有发 ark 消息权限", "permission", False),
    304005: _e(304005, "EMBED_LIMIT", "embed 长度超限", "request", False),
    304006: _e(304006, "SERVER_CONFIG", "后台配置错误", "platform", True),
    304007: _e(304007, "GET_GUILD", "查询频道异常", "resource", True),
    304008: _e(304008, "GET_BOT", "查询机器人异常", "resource", True),
    304009: _e(304009, "GET_CHENNAL", "查询子频道异常", "resource", True),
    304010: _e(304010, "CHANGE_IMAGE_URL", "图片转存错误", "media", True),
    304011: _e(304011, "NO_TEMPLATE", "模板不存在", "resource", False),
    304012: _e(304012, "GET_TEMPLATE", "取模板错误", "platform", True),
    304014: _e(304014, "TEMPLATE_PRIVILEGE", "没有模板权限", "permission", False),
    304016: _e(304016, "SEND_ERROR", "发消息错误", "send", True),
    304017: _e(304017, "UPLOAD_IMAGE", "图片上传错误", "media", True),
    304018: _e(304018, "SESSION_NOT_EXIST", "机器人没连上 gateway", "gateway", True),
    304019: _e(304019, "AT_EVERYONE_TIMES", "@全体成员次数超限", "rate_limit", False),
    304020: _e(304020, "FILE_SIZE", "文件大小超限", "media", False),
    304021: _e(304021, "GET_FILE", "下载文件错误", "media", True),
    304022: _e(304022, "PUSH_TIME", "推送消息时间限制", "send", False),
    304023: _e(304023, "PUSH_MSG_ASYNC_OK", "推送消息异步调用成功，等待人工审核", "async", False),
    304024: _e(304024, "REPLY_MSG_ASYNC_OK", "回复消息异步调用成功，等待人工审核", "async", False),
    304025: _e(304025, "BEAT", "消息被打击", "safety", False),
    304026: _e(304026, "MSG_ID", "回复的消息 id 错误", "reply", False),
    304027: _e(304027, "MSG_EXPIRE", "回复的消息过期", "reply", False),
    304028: _e(304028, "MSG_PROTECT", "非 At 当前用户的消息不允许回复", "reply", False),
    304029: _e(304029, "CORPUS_ERROR", "调语料服务错误", "platform", True),
    304030: _e(304030, "CORPUS_NOT_MATCH", "语料不匹配", "content", False),
    304031: _e(304031, "DM_CLOSED", "私信已关闭", "send", False),
    304032: _e(304032, "DM_NOT_EXIST", "私信不存在", "resource", False),
    304033: _e(304033, "DM_CREATE_ERROR", "拉私信错误", "send", True),
    304034: _e(304034, "NOT_DM_MEMBER", "不是私信成员", "permission", False),
    304035: _e(304035, "PUSH_CHANNEL_LIMIT", "推送消息超过子频道数量限制", "rate_limit", False),
    304036: _e(304036, "NO_MARKDOWN_TEMPLATE_PERMISSION", "没有 markdown 模板权限", "permission", False),
    304037: _e(304037, "NO_KEYBOARD_PERMISSION", "没有发消息按钮组件的权限", "permission", False),
    304038: _e(304038, "KEYBOARD_NOT_EXIST", "消息按钮组件不存在", "resource", False),
    304039: _e(304039, "KEYBOARD_PARSE_ERROR", "消息按钮组件解析错误", "request", False),
    304040: _e(304040, "KEYBOARD_CONTENT_ERROR", "消息按钮组件消息内容错误", "request", False),
    304044: _e(304044, "GET_MESSAGE_SETTING_ERROR", "取消息设置错误", "platform", True),
    304045: _e(304045, "CHANNEL_PUSH_LIMIT", "子频道主动消息数限频", "rate_limit", False),
    304046: _e(304046, "CHANNEL_PUSH_FORBIDDEN", "不允许在此子频道发主动消息", "permission", False),
    304047: _e(304047, "GUILD_PUSH_CHANNEL_LIMIT", "主动消息推送超过限制的子频道数", "rate_limit", False),
    304048: _e(304048, "GUILD_PUSH_FORBIDDEN", "不允许在此频道发主动消息", "permission", False),
    304049: _e(304049, "DM_PUSH_LIMIT", "私信主动消息数限频", "rate_limit", False),
    304050: _e(304050, "DM_PUSH_TOTAL_LIMIT", "私信主动消息总量限频", "rate_limit", False),
    304051: _e(304051, "GUIDE_REQUEST_BUILD_ERROR", "消息设置引导请求构造错误", "request", False),
    304052: _e(304052, "GUIDE_RATE_LIMIT", "发消息设置引导超频", "rate_limit", True),
    306001: _e(306001, "DeleteMessageParamInvalid", "撤回消息参数错误", "delete_message", False),
    306002: _e(306002, "DeleteMessageIdError", "消息 id 错误", "delete_message", False),
    306003: _e(306003, "DeleteGetMessageFailed", "获取消息错误", "delete_message", True),
    306004: _e(306004, "DeletePermissionDenied", "没有撤回此消息的权限", "delete_message", False),
    306005: _e(306005, "DeleteMessageFailed", "消息撤回失败", "delete_message", True),
    306006: _e(306006, "DeleteGetChannelFailed", "获取子频道失败", "delete_message", True),
    306007: _e(306007, "DeleteWrongGroup", "非当前群的消息", "delete_message", False),
    306008: _e(306008, "DeleteNotOwnBotMessage", "非当前机器人发送的消息", "delete_message", False),
    306009: _e(306009, "DeleteNotCurrentUserDM", "非与当前用户发送的消息", "delete_message", False),
    306010: _e(306010, "DeleteInternalError", "内部错误", "delete_message", True),
    306011: _e(306011, "DeleteMessageExpired", "超出可撤回消息时间", "delete_message", False),
    501001: _e(501001, "AnnouncementParamInvalid", "参数校验失败", "announce", False),
    501002: _e(501002, "AnnouncementChannelCreateFailed", "创建子频道公告失败", "announce", True),
    501003: _e(501003, "AnnouncementChannelDeleteFailed", "删除子频道公告失败", "announce", True),
    501004: _e(501004, "AnnouncementGetGuildFailed", "获取频道信息失败", "announce", True),
    501005: _e(501005, "AnnouncementMessageIdError", "MessageID 错误", "announce", False),
    501006: _e(501006, "AnnouncementGuildCreateFailed", "创建频道全局公告失败", "announce", True),
    501007: _e(501007, "AnnouncementGuildDeleteFailed", "删除频道全局公告失败", "announce", True),
    501008: _e(501008, "AnnouncementMessageIdMissing", "MessageID 不存在", "announce", False),
    501009: _e(501009, "AnnouncementMessageIdParseFailed", "MessageID 解析失败", "announce", False),
    501010: _e(501010, "AnnouncementNotChannelMessage", "此条消息非子频道内消息", "announce", False),
    501011: _e(501011, "PinMessageCreateFailed", "创建精华消息失败", "announce", True),
    501012: _e(501012, "PinMessageDeleteFailed", "删除精华消息失败", "announce", True),
    501013: _e(501013, "PinMessageLimit", "精华消息超过最大数量", "announce", False),
    501014: _e(501014, "AnnouncementSafetyBlocked", "安全打击", "announce", False),
    501015: _e(501015, "AnnouncementNotAllowed", "此消息不允许设置", "announce", False),
    501016: _e(501016, "RecommendChannelLimit", "频道公告子频道推荐超过最大数量", "announce", False),
    501017: _e(501017, "NotGuildOwnerOrAdmin", "非频道主或管理员", "announce", False),
    501018: _e(501018, "RecommendChannelInvalid", "推荐子频道 ID 无效", "announce", False),
    501019: _e(501019, "AnnouncementTypeError", "公告类型错误", "announce", False),
    501020: _e(501020, "RecommendChannelAnnouncementCreateFailed", "创建推荐子频道类型频道公告失败", "announce", True),
    502001: _e(502001, "MuteGuildIdInvalid", "频道 id 无效", "mute", False),
    502002: _e(502002, "MuteGuildIdEmpty", "频道 id 为空", "mute", False),
    502003: _e(502003, "MuteUserIdInvalid", "用户 id 无效", "mute", False),
    502004: _e(502004, "MuteUserIdEmpty", "用户 id 为空", "mute", False),
    502005: _e(502005, "MuteTimestampInvalid", "timestamp 不合法", "mute", False),
    502006: _e(502006, "MuteTimestampWrong", "timestamp 无效", "mute", False),
    502007: _e(502007, "MuteParamConvertError", "参数转换错误", "mute", False),
    502008: _e(502008, "MuteRPCFailed", "rpc 调用失败", "mute", True),
    502009: _e(502009, "MuteSafetyBlocked", "安全打击", "mute", False),
    502010: _e(502010, "MuteHeaderError", "请求头错误", "mute", False),
    503001: _e(503001, "ForumGuildIdInvalid", "频道 id 无效", "forum", False),
    503002: _e(503002, "ForumGuildIdEmpty", "频道 id 为空", "forum", False),
    503003: _e(503003, "ForumGetChannelFailed", "获取子频道信息失败", "forum", True),
    503004: _e(503004, "ForumRateLimit", "超出发布帖子的频次限制", "forum", True),
    503005: _e(503005, "ForumTitleEmpty", "帖子标题为空", "forum", False),
    503006: _e(503006, "ForumContentEmpty", "帖子内容为空", "forum", False),
    503007: _e(503007, "ForumPostIdEmpty", "帖子ID为空", "forum", False),
    503008: _e(503008, "ForumGetXUinFailed", "获取X-Uin失败", "forum", True),
    503009: _e(503009, "ForumPostIdInvalid", "帖子ID无效或不合法", "forum", False),
    503010: _e(503010, "ForumTinyIdConvertFailed", "通过Uin获取TinyID失败", "forum", True),
    503011: _e(503011, "ForumTimestampInvalid", "帖子ID时间戳无效或不合法", "forum", False),
    503012: _e(503012, "ForumPostMissing", "帖子不存在或已删除", "forum", False),
    503013: _e(503013, "ForumInternalError", "服务器内部错误", "forum", True),
    503014: _e(503014, "ForumJsonParseFailed", "帖子JSON内容解析失败", "forum", False),
    503015: _e(503015, "ForumContentConvertFailed", "帖子内容转换失败", "forum", False),
    503016: _e(503016, "ForumLinkLimit", "链接数量超过限制", "forum", False),
    503017: _e(503017, "ForumTextLimit", "字数超过限制", "forum", False),
    503018: _e(503018, "ForumImageLimit", "图片数量超过限制", "forum", False),
    503019: _e(503019, "ForumVideoLimit", "视频数量超过限制", "forum", False),
    503020: _e(503020, "ForumTitleLimit", "标题长度超过限制", "forum", False),
    504001: _e(504001, "RateLimitParamInvalid", "请求参数无效错误", "rate_limit", False),
    504002: _e(504002, "RateLimitGetHeaderFailed", "获取 HTTP 头失败", "rate_limit", True),
    504003: _e(504003, "RateLimitGetBotUinFailed", "获取 BOT UIN 错误", "rate_limit", True),
    504004: _e(504004, "RateLimitSettingsFailed", "获取消息频率设置信息错误", "rate_limit", True),
    610001: _e(610001, "APIPermissionGetGuildIdFailed", "获取频道 ID 失败", "guild_permission", True),
    610002: _e(610002, "APIPermissionGetHeaderFailed", "获取 HTTP 头失败", "guild_permission", True),
    610003: _e(610003, "APIPermissionGetBotNumberFailed", "获取机器人号码失败", "guild_permission", True),
    610004: _e(610004, "APIPermissionGetBotRoleFailed", "获取机器人角色失败", "guild_permission", True),
    610005: _e(610005, "APIPermissionGetBotRoleInternalError", "获取机器人角色内部错误", "guild_permission", True),
    610006: _e(610006, "APIPermissionListFailed", "拉取机器人权限列表失败", "guild_permission", True),
    610007: _e(610007, "BotNotInGuild", "机器人不在频道内", "guild_permission", False),
    610008: _e(610008, "APIPermissionInvalidParam", "无效参数", "guild_permission", False),
    610009: _e(610009, "APIPermissionDetailFailed", "获取 API 接口详情失败", "guild_permission", True),
    610010: _e(610010, "APIPermissionAlreadyGranted", "API 接口已授权", "guild_permission", False),
    610011: _e(610011, "APIPermissionBotInfoFailed", "获取机器人信息失败", "guild_permission", True),
    610012: _e(610012, "APIPermissionRateLimitFailed", "限频失败", "guild_permission", True),
    610013: _e(610013, "APIPermissionRateLimited", "已限频", "guild_permission", True),
    610014: _e(610014, "APIPermissionLinkSendFailed", "api 授权链接发送失败", "guild_permission", True),
    620001: _e(620001, "ReactionInvalidParam", "表情表态无效参数", "reaction", False),
    620002: _e(620002, "ReactionTypeLimit", "表情反应类型数量上限", "reaction", False),
    620003: _e(620003, "ReactionAlreadySet", "已经设置过该表情表态", "reaction", False),
    620004: _e(620004, "ReactionNotSet", "没有设置过该表情表态", "reaction", False),
    620005: _e(620005, "ReactionNoPermission", "没有权限设置表情表态", "reaction", False),
    620006: _e(620006, "ReactionRateLimited", "操作限频", "reaction", True),
    620007: _e(620007, "ReactionFailed", "表情表态操作失败", "reaction", True),
    630001: _e(630001, "InteractionDataInvalidParam", "互动回调数据更新无效参数", "interaction", False),
    630002: _e(630002, "InteractionDataGetAppIdFailed", "互动回调数据更新获取AppID失败", "interaction", True),
    630003: _e(630003, "InteractionDataAppIdMismatch", "互动回调数据 AppID 不匹配", "interaction", False),
    630004: _e(630004, "InteractionDataStoreFailed", "互动回调数据更新内部存储错误", "interaction", True),
    630005: _e(630005, "InteractionDataStoreReadFailed", "互动回调数据更新内部存储读取错误", "interaction", True),
    630006: _e(630006, "InteractionDataRequestAppIdFailed", "互动回调数据更新读取请求AppID失败", "interaction", True),
    630007: _e(630007, "InteractionDataTooLarge", "互动回调数据太大", "interaction", False),
    1100100: _e(1100100, "SAFE_RATE_LIMIT", "安全打击：消息被限频", "rate_limit", True),
    1100101: _e(1100101, "SAFE_SENSITIVE", "安全打击：内容涉及敏感", "safety", False),
    1100102: _e(1100102, "SAFE_NOT_ELIGIBLE", "暂未获得新功能体验资格", "permission", False),
    1100103: _e(1100103, "SAFE_BLOCK", "安全打击", "safety", False),
    1100104: _e(1100104, "GROUP_INVALID", "该群已失效或当前群已不存在", "resource", False),
    1100300: _e(1100300, "INTERNAL_ERROR", "系统内部错误", "platform", True),
    1100301: _e(1100301, "CALLER_NOT_GROUP_MEMBER", "调用方不是群成员", "permission", False),
    1100302: _e(1100302, "GET_CHANNEL_NAME_FAILED", "获取指定频道名称失败", "platform", True),
    1100303: _e(1100303, "NON_ADMIN_CANNOT_SPEAK", "主页频道非管理员不允许发消息", "permission", False),
    1100304: _e(1100304, "AT_TIMES_AUTH_FAILED", "@次数鉴权失败", "platform", True),
    1100305: _e(1100305, "TINYID_TO_UIN_FAILED", "TinyId 转换 Uin 失败", "platform", True),
    1100306: _e(1100306, "NOT_PRIVATE_GUILD_MEMBER", "非私有频道成员", "permission", False),
    1100307: _e(1100307, "NOT_WHITELIST_CHANNEL_APP", "非白名单应用子频道", "permission", False),
    1100308: _e(1100308, "CHANNEL_RATE_LIMIT", "触发频道内限频", "rate_limit", True),
    1100499: _e(1100499, "OTHER_SEND_ERROR", "其他错误", "send", True),
    3300006: _e(3300006, "EDIT_MESSAGE_SAFETY_BLOCK", "编辑消息安全打击", "edit_message", False),
}

KNOWN_ERROR_RANGES: tuple[tuple[range, str, str, bool], ...] = (
    (range(301000, 301100), "channel_permission", "子频道权限错误", False),
    (range(302000, 302100), "schedule", "日程相关错误", False),
    (range(304000, 304100), "send", "消息发送相关错误", False),
    (range(306001, 306012), "delete_message", "撤回消息错误", True),
    (range(501000, 502000), "announce", "公告错误", True),
    (range(502000, 503000), "mute", "禁言相关错误", True),
    (range(503000, 504000), "forum", "帖子/论坛相关错误", False),
    (range(504000, 505000), "rate_limit", "消息频率相关错误", True),
    (range(610000, 620000), "guild_permission", "频道权限错误", False),
    (range(620000, 630000), "reaction", "表情表态错误", False),
    (range(630000, 640000), "interaction", "互动回调数据更新错误", True),
    (range(1000000, 3000000), "send", "发消息错误", False),
    (range(3000000, 4000000), "edit_message", "编辑消息错误", False),
)


def build_openapi_error(
    response: httpx.Response,
    payload: Any,
    *,
    default_message: str | None = None,
) -> QQOpenAPIError:
    error_code = extract_business_code(payload)
    known = lookup_known_error(error_code)
    error_message = default_message or response.reason_phrase or http_status_description(response.status_code)
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("msg")
        if message:
            error_message = str(message)
    elif isinstance(payload, str) and payload.strip():
        error_message = payload.strip()
    elif known is not None:
        error_message = known.description

    return QQOpenAPIError(
        status_code=response.status_code,
        trace_id=trace_id_from_response(response),
        error_code=error_code,
        error_message=error_message,
        response_body=payload,
        known=known,
    )


def trace_id_from_response(response: httpx.Response) -> str | None:
    trace_id = response.headers.get("X-Tps-trace-ID", "").strip()
    return trace_id or None


def http_status_description(status_code: int) -> str:
    return HTTP_STATUS_DESCRIPTIONS.get(status_code, "request failed")


def lookup_known_error(error_code: int | None) -> QQKnownOpenAPIError | None:
    if error_code is None:
        return None
    known = KNOWN_OPENAPI_ERRORS.get(error_code)
    if known is not None:
        return known
    for code_range, category, description, retryable in KNOWN_ERROR_RANGES:
        if error_code in code_range:
            return QQKnownOpenAPIError(
                code=error_code,
                name=f"{category.upper()}_{error_code}",
                description=description,
                category=category,
                retryable=retryable,
            )
    return None


def extract_business_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("code")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
