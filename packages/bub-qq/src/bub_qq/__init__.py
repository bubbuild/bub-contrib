"""QQ Open Platform channel for Bub.

Package layout:

- ``protocol``: auth, OpenAPI client, signatures, and event models
- ``gateway``: webhook / websocket receive transports
- ``inbound``: QQ events adapted to Bub ``ChannelMessage``
- ``outbound``: Bub messages delivered through QQ OpenAPI
"""

from __future__ import annotations

from .channel import QQChannel
from .config import QQConfig
from .gateway import QQGatewayInfo
from .gateway import QQSessionStartLimit
from .gateway import QQWebhookServer
from .gateway import QQWebSocketClient
from .protocol import QQC2CMessage
from .protocol import QQGroupMessage
from .protocol import QQOpenAPI
from .protocol import QQOpenAPIError
from .protocol import QQTokenProvider

__all__ = [
    "QQChannel",
    "QQConfig",
    "QQGatewayInfo",
    "QQOpenAPI",
    "QQOpenAPIError",
    "QQSessionStartLimit",
    "QQTokenProvider",
    "QQWebhookServer",
    "QQWebSocketClient",
    "QQC2CMessage",
    "QQGroupMessage",
]
