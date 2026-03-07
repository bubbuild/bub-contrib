from __future__ import annotations

__all__ = ["FeishuChannel"]


def __getattr__(name: str):
    if name != "FeishuChannel":
        raise AttributeError(name)
    from .channel import FeishuChannel

    return FeishuChannel
