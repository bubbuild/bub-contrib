"""Turn a sandbox policy into a command line for Seatbelt (macOS) or bubblewrap (Linux).

Both backends enforce the same policy: the whole filesystem is readable except
``deny_read``; only ``writable`` paths can be written; the network is off
unless ``network`` is set. The more specific path wins: a writable root inside
a denied path stays usable, and a denied path inside a writable root stays
denied (for writes too).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

_SEATBELT_BASE = """\
(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow user-preference-read)
(allow ipc-posix-sem)
(allow ipc-posix-shm-read-data)
(allow pseudo-tty)
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.system.notification_center")
  (global-name "com.apple.system.logger")
  (global-name "com.apple.logd")
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.FSEvents"))
(allow file-read*)
(allow file-write-data file-ioctl
  (literal "/dev/null")
  (literal "/dev/zero")
  (literal "/dev/tty")
  (regex #"^/dev/fd/[0-9]+$"))
"""

_SEATBELT_NETWORK = """\
(allow network*)
(allow mach-lookup
  (global-name "com.apple.SystemConfiguration.configd")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.dnssd.service")
  (global-name "com.apple.networkd")
  (global-name "com.apple.nsurlsessiond")
  (global-name "com.apple.trustd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.ocspd"))
"""


@dataclass(frozen=True)
class Policy:
    """What a sandboxed process may do. Paths are absolute and resolved."""

    writable: tuple[str, ...]
    deny_read: tuple[str, ...] = ()
    network: bool = False

    def _denied(self, *, nested: bool) -> tuple[str, ...]:
        return tuple(
            path
            for path in self.deny_read
            if any(_within(path, root) for root in self.writable) == nested
        )

    @property
    def outer_denied(self) -> tuple[str, ...]:
        """Denied paths outside every writable root: writable roots inside them win."""

        return self._denied(nested=False)

    @property
    def inner_denied(self) -> tuple[str, ...]:
        """Denied paths inside a writable root: they win over the root."""

        return self._denied(nested=True)


def _within(path: str, root: str) -> bool:
    # Strictly inside: denying a writable root itself leaves the root usable.
    return path.startswith(root.rstrip("/") + "/")


def _subpaths(prefix: str, count: int, start: int = 0) -> str:
    return " ".join(
        f'(subpath (param "{prefix}{index}"))' for index in range(start, start + count)
    )


def seatbelt_profile(policy: Policy) -> str:
    """The SBPL profile; paths are passed as ``-D`` parameters, never spliced in."""

    # Later rules win in SBPL; DENY<n> follow outer_denied then inner_denied.
    outer, inner = len(policy.outer_denied), len(policy.inner_denied)
    parts = [_SEATBELT_BASE]
    if outer:
        parts.append(f"(deny file-read* {_subpaths('DENY', outer)})\n")
    if policy.writable:
        writable = _subpaths("WRITE", len(policy.writable))
        parts.append(f"(allow file-read* file-write* {writable})\n")
    if inner:
        denied = _subpaths("DENY", inner, start=outer)
        parts.append(f"(deny file-read* file-write* {denied})\n")
    if policy.network:
        parts.append(_SEATBELT_NETWORK)
    return "".join(parts)


def seatbelt_command(policy: Policy, argv: Sequence[str]) -> list[str]:
    args = [SANDBOX_EXEC, "-p", seatbelt_profile(policy)]
    for index, path in enumerate(policy.outer_denied + policy.inner_denied):
        args += ["-D", f"DENY{index}={path}"]
    for index, path in enumerate(policy.writable):
        args += ["-D", f"WRITE{index}={path}"]
    return [*args, "--", *argv]


def bwrap_command(
    policy: Policy,
    argv: Sequence[str],
    *,
    tmpdir: str,
    cwd: str,
    bwrap: str = "bwrap",
) -> list[str]:
    """bubblewrap arguments; ``tmpdir`` becomes the sandbox's private ``/tmp``.

    No ``--new-session``: the process must stay in the process group Bub
    created, so stopping it reaches everything it started.
    """

    args = [
        bwrap,
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--bind", tmpdir, "/tmp",
    ]  # fmt: skip
    if not policy.network:
        args.append("--unshare-net")
    # Later mounts cover earlier ones, in the same order as the Seatbelt rules.
    args += _bwrap_masks(policy.outer_denied)
    for path in policy.writable:
        if path != tmpdir:
            args += ["--bind-try", path, path]
    args += _bwrap_masks(policy.inner_denied)
    return [*args, "--chdir", cwd, "--", *argv]


def _bwrap_masks(paths: Sequence[str]) -> list[str]:
    args: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            args += ["--tmpfs", path, "--remount-ro", path]
        elif os.path.exists(path):
            args += ["--ro-bind", "/dev/null", path]
    return args
