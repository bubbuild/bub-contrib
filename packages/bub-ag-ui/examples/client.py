from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from ag_ui.core import Context, RunAgentInput, UserMessage

DEFAULT_ENDPOINT = "http://127.0.0.1:8088/agent"


def build_run_input(prompt: str) -> RunAgentInput:
    """Build one minimal, protocol-native AG-UI run request."""
    request_id = uuid4().hex
    return RunAgentInput(
        thread_id=f"example-thread-{request_id}",
        run_id=f"example-run-{request_id}",
        parent_run_id=None,
        state={"example": "bub-ag-ui"},
        messages=[UserMessage(id=f"user-{request_id}", content=prompt)],
        tools=[],
        context=[Context(description="client", value="bub-ag-ui example")],
        forwarded_props={},
    )


def decode_sse_events(lines: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Decode the JSON `data` records emitted by the example endpoint."""
    for raw_line in lines:
        line = raw_line.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").lstrip()
        event = json.loads(payload)
        if isinstance(event, dict):
            yield event


def stream_run(
    endpoint: str, input_data: RunAgentInput, *, timeout: float
) -> Iterator[dict[str, Any]]:
    """Send one AG-UI run and yield its server-sent events."""
    request = Request(
        endpoint,
        data=input_data.model_dump_json(by_alias=True, exclude_none=True).encode(
            "utf-8"
        ),
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        yield from decode_sse_events(response)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send one AG-UI request through the Bub gateway."
    )
    parser.add_argument("prompt", help="Message for the Bub agent")
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"AG-UI endpoint (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="HTTP timeout in seconds (default: 120)",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        for event in stream_run(
            args.endpoint, build_run_input(args.prompt), timeout=args.timeout
        ):
            print(json.dumps(event, ensure_ascii=False))
    except URLError as exc:
        raise SystemExit(
            f"Could not reach the Bub AG-UI endpoint at {args.endpoint}: {exc}"
        ) from exc


if __name__ == "__main__":
    main()
