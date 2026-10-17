"""A Bub :class:`~bub.environment.Environment` that runs everything in a local OS sandbox.

Processes run on the host, wrapped in ``sandbox-exec`` (macOS Seatbelt) or
``bwrap`` (Linux bubblewrap). File access from ``fs.*`` goes through the same
sandbox, so the tools and the shell see one policy. Each process starts its own
session, as with Bub's host environment, so stopping it reaches the whole tree.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path

import bub
from bub.builtin.environment import LocalEnvironment, LocalProcess
from loguru import logger

from .config import SandboxEnvConfig
from .policy import SANDBOX_EXEC, Policy, bwrap_command, seatbelt_command


class SandboxError(RuntimeError):
    """The sandbox cannot run on this host."""


def expand(path: str) -> str:
    """Expand ``~`` and ``$BUB_HOME`` and resolve symlinks (Seatbelt matches real paths)."""

    if path == "$BUB_HOME" or path.startswith("$BUB_HOME/"):
        path = str(bub.home) + path.removeprefix("$BUB_HOME")
    return os.path.realpath(os.path.expanduser(path))


@cache
def darwin_user_dirs() -> tuple[str, ...]:
    """The per-user temp and cache directories macOS tools fall back on (``xcrun``, ``git``)."""

    dirs = []
    for name in ("DARWIN_USER_TEMP_DIR", "DARWIN_USER_CACHE_DIR"):
        try:
            result = subprocess.run(
                ["getconf", name], capture_output=True, text=True, check=True
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        if value := result.stdout.strip():
            dirs.append(os.path.realpath(value))
    return tuple(dirs)


def scrub_env(environ: Mapping[str, str], patterns: Sequence[str]) -> dict[str, str]:
    """``environ`` without variables whose names match ``patterns`` (case insensitive)."""

    upper = [pattern.upper() for pattern in patterns]
    return {
        key: value
        for key, value in environ.items()
        if not any(fnmatchcase(key.upper(), pattern) for pattern in upper)
    }


def _os_error(stderr: bytes, path: str) -> OSError:
    message = stderr.decode(errors="replace").strip() or path
    if "No such file" in message:
        return FileNotFoundError(message)
    if any(
        marker in message
        for marker in ("not permitted", "Permission denied", "Read-only file system")
    ):
        return PermissionError(message)
    return OSError(message)


class SandboxEnvironment(LocalEnvironment):
    """Run a session's processes and file access in a local OS sandbox."""

    def __init__(self, config: SandboxEnvConfig, *, workspace: Path) -> None:
        super().__init__(workspace.resolve())
        self.config = config
        self.backend = config.resolved_backend()
        self.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix="bub-sandbox-"))
        writable = [str(self.workspace), self.tmpdir]
        if self.backend == "seatbelt":
            writable += darwin_user_dirs()
        writable += [expand(path) for path in config.writable]
        self.policy = Policy(
            writable=tuple(dict.fromkeys(writable)),
            deny_read=tuple(dict.fromkeys(expand(path) for path in config.deny_read)),
            network=config.network,
        )

    def wrap(self, argv: Sequence[str], cwd: str) -> list[str]:
        """``argv`` wrapped in the sandbox; raises :class:`SandboxError` if it cannot run here."""

        if self.backend == "seatbelt":
            if sys.platform != "darwin" or not os.access(SANDBOX_EXEC, os.X_OK):
                raise SandboxError(f"Seatbelt needs macOS with {SANDBOX_EXEC}")
            return seatbelt_command(self.policy, argv)
        bwrap = shutil.which(self.config.bwrap)
        if bwrap is None:
            raise SandboxError(
                f"bubblewrap not found ({self.config.bwrap}); install it, e.g."
                " `apt install bubblewrap`"
            )
        return bwrap_command(
            self.policy, argv, tmpdir=self.tmpdir, cwd=cwd, bwrap=bwrap
        )

    def process_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = scrub_env(os.environ, self.config.env_deny)
        # bwrap mounts the session temp dir at /tmp.
        env["TMPDIR"] = self.tmpdir if self.backend == "seatbelt" else "/tmp"
        return {**env, **(extra or {})}

    async def spawn(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> LocalProcess:
        if isinstance(command, str):
            argv = [self.SHELL or "/bin/sh", "-c", command]
        else:
            argv = list(command)
        cwd = cwd or str(self.workspace)
        process = await asyncio.create_subprocess_exec(
            *self.wrap(argv, cwd),
            cwd=cwd,
            env=self.process_env(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return LocalProcess(process)

    async def _run(
        self, argv: Sequence[str], stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        process = await self.spawn(argv)
        if stdin is not None:
            await process.write_stdin(stdin)
        process.close_stdin()
        stdout, stderr = await asyncio.gather(
            process.stdout.read(), process.stderr.read()
        )
        return await process.wait(), stdout, stderr

    async def read_bytes(self, path: str) -> bytes:
        code, stdout, stderr = await self._run(["cat", "--", path])
        if code != 0:
            raise _os_error(stderr, path)
        return stdout

    async def read_text(self, path: str) -> str:
        return (await self.read_bytes(path)).decode("utf-8")

    async def write_text(self, path: str, content: str) -> None:
        script = 'mkdir -p -- "$(dirname -- "$0")" && cat > "$0"'
        code, _, stderr = await self._run(
            ["/bin/sh", "-c", script, path], stdin=content.encode("utf-8")
        )
        if code != 0:
            raise _os_error(stderr, path)

    async def close(self) -> None:
        await asyncio.to_thread(shutil.rmtree, self.tmpdir, True)
        logger.info("sandbox_env.closed tmpdir={}", self.tmpdir)
