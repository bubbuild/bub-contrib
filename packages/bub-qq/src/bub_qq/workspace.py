"""Workspace paths: where QQ file tools may act and where artifacts live.

The framework puts ``_runtime_workspace`` on turn state (cwd, or
``bub --workspace``). File tools stay inside it, protected files inside it
are never touched, and plugin-written files go under ``inbox/`` and
``outbox/``. Shell commands are not inspected here: string analysis cannot
confine a shell, so shell access is an explicit Guard decision instead.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlparse

from bub.hooks.interception import ToolCall
from bub.turn import TurnState
from loguru import logger

INBOX_NAME = "inbox"
OUTBOX_NAME = "outbox"
MAX_INBOUND_DOWNLOAD_BYTES = 200 * 1024 * 1024

_FS_TOOLS = frozenset(
    {"fs.read", "fs.write", "fs.edit", "fs_read", "fs_write", "fs_edit"}
)
_SENSITIVE_DIR_NAMES = frozenset({".git"})


def workspace_from_state(state: TurnState | dict[str, Any] | None) -> Path:
    raw = state.get("_runtime_workspace") if isinstance(state, dict) else None
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def path_inside_workspace(path: Path, workspace: Path) -> bool:
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return False
    return True


def resolve_in_workspace(
    raw: str,
    workspace: Path,
    *,
    cwd: Path | None = None,
) -> Path | None:
    """Resolve ``raw``; return None when it escapes ``workspace``."""

    text = raw.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    base = cwd if cwd is not None else workspace
    resolved = path.resolve() if path.is_absolute() else (base / path).resolve()
    if not path_inside_workspace(resolved, workspace):
        return None
    return resolved


def is_unsafe_artifact_workspace(workspace: Path) -> bool:
    """True when writing ``inbox/`` at workspace root would be a bad default.

    ``/`` and ``$HOME`` are writable for some users but are not a dedicated
    work directory. Unwritable roots also fall back.
    """

    root = workspace.expanduser().resolve()
    if root == Path(root.anchor):
        return True
    if root == Path.home().resolve():
        return True
    return not os.access(root, os.W_OK)


def artifact_root(workspace: Path) -> Path:
    """Where bub-qq writes inbound/generated files.

    Dedicated workspaces (the recommended ``cd`` target) get ``inbox/`` and
    ``outbox/`` at the workspace root. ``/``, ``$HOME``, and unwritable
    directories fall back to ``<bub home>/qq`` so files never scatter at
    the filesystem root.
    """

    root = workspace.expanduser().resolve()
    if not is_unsafe_artifact_workspace(root):
        return root
    import bub

    fallback = (bub.home / "qq").expanduser().resolve()
    logger.info(
        "qq.workspace.artifact_fallback workspace={} dest={}",
        root,
        fallback,
    )
    return fallback


def inbox_dir(workspace: Path, message_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", message_id)[:64] or "message"
    return artifact_root(workspace) / INBOX_NAME / safe_id


def outbox_dir(workspace: Path) -> Path:
    return artifact_root(workspace) / OUTBOX_NAME


def safe_filename(name: str | None, url: str | None, index: int) -> str:
    raw = (name or "").strip() or Path(unquote(urlparse(url or "").path)).name
    raw = Path(raw).name.strip()
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    return raw or f"file-{index}"


def sensitive_path_reason(path: Path, *, extra: Iterable[Path] = ()) -> str | None:
    """Denial message when ``path`` is a protected file, else None.

    Protected files hold credentials or plugin state: ``.env`` files (Bub
    and bub-qq load secrets from them), anything under ``.git/``, and the
    ``extra`` paths (the QQ state file). The check runs on the resolved
    path, so a symlink pointing at a protected file is caught too. No
    identity, approval or config can unlock these.
    """

    resolved = path.expanduser().resolve()
    name = resolved.name
    protected = (
        name == ".env"
        or name.startswith(".env.")
        or any(part in _SENSITIVE_DIR_NAMES for part in resolved.parts)
        or any(resolved == other.expanduser().resolve() for other in extra)
    )
    if not protected:
        return None
    return (
        f"'{path}' is a protected file (.env, .git/ or QQ state) and cannot be"
        " read, written or sent by QQ tools."
    )


def media_path_reason(path: Path, workspace: Path) -> str | None:
    """Denial message unless ``path`` may be sent to the chat as media.

    Only files under ``outbox/`` (plugin downloads, generated files) and
    ``inbox/`` (saved attachments) qualify; anything else in the workspace
    could be source code or secrets.
    """

    resolved = path.expanduser().resolve()
    sensitive = sensitive_path_reason(resolved)
    if sensitive is not None:
        return sensitive
    root = artifact_root(workspace)
    for allowed in (root / OUTBOX_NAME, root / INBOX_NAME):
        if resolved.is_relative_to(allowed):
            return None
    return (
        f"media_path must be a file under {root / OUTBOX_NAME}/ or"
        f" {root / INBOX_NAME}/."
    )


def tool_protected_reason(
    call: ToolCall, workspace: Path, *, extra: Iterable[Path] = ()
) -> str | None:
    """Hard denial for protected files and disallowed media, else None.

    The Guard checks this before anything else, so it never becomes an
    approval request and no identity bypasses it.
    """

    args = call.arguments if isinstance(call.arguments, dict) else {}
    if call.tool in _FS_TOOLS:
        raw = args.get("path")
        if raw is None or not str(raw).strip():
            return None
        return sensitive_path_reason(
            _resolve_from(str(raw).strip(), workspace), extra=extra
        )
    if call.tool in {"qq.send", "qq_send"}:
        raw = args.get("media_path")
        if raw is None or not str(raw).strip():
            return None
        resolved = _resolve_from(str(raw).strip(), workspace)
        return sensitive_path_reason(resolved, extra=extra) or media_path_reason(
            resolved, workspace
        )
    return None


def _resolve_from(raw: str, workspace: Path) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()
