from __future__ import annotations

import sys
from collections.abc import Sequence

_PASSTHROUGH_COMMANDS = frozenset({"acp", "login", "onboard"})


def _resolve_args(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    if args and args[0] in _PASSTHROUGH_COMMANDS:
        return args
    return ["acp", *args]


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point for running Bub's ACP server via ``uvx bub-acp-server``."""
    from bub.__main__ import create_cli_app

    args = _resolve_args(sys.argv[1:] if argv is None else argv)
    create_cli_app()(args=args, prog_name="bub-acp-server")


if __name__ == "__main__":
    main()
