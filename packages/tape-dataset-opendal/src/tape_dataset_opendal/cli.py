from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Annotated, Any

import opendal
import typer
from bub.framework import BubFramework
from republic.tape.store import AsyncTapeStore, TapeStore, is_async_tape_store

from tape_dataset_opendal import export_dataset, export_dataset_async
from tape_dataset_opendal.filters import EntryFilter
from tape_dataset_opendal.models import ExportLayout, ExportReport

RootOption = Annotated[
    str,
    typer.Option(
        "--root",
        help="Dataset root path inside the target OpenDAL backend",
    ),
]
SchemeOption = Annotated[
    str,
    typer.Option(
        "--scheme",
        help="OpenDAL scheme, for example fs, s3, azblob, gcs, memory",
    ),
]
ConfigOption = Annotated[
    list[str] | None,
    typer.Option(
        "--config",
        help="OpenDAL operator config in key=value form, repeatable",
    ),
]
NoSegmentsOption = Annotated[
    bool,
    typer.Option(
        "--no-segments",
        help="Skip anchor-derived segments.jsonl export",
    ),
]
NoRawOption = Annotated[
    bool,
    typer.Option(
        "--no-raw",
        help="Skip per-tape raw JSONL export",
    ),
]
FilterOption = Annotated[
    list[str] | None,
    typer.Option(
        "--filter",
        help="CEL filter expression applied to each tape entry, repeatable",
    ),
]
FilterFileOption = Annotated[
    list[str] | None,
    typer.Option(
        "--filter-file",
        help="Path to a file containing CEL filter expressions, one per line",
    ),
]


def export_command(
    ctx: typer.Context,
    scheme: SchemeOption = "fs",
    root: RootOption = "",
    config: ConfigOption = None,
    no_segments: NoSegmentsOption = False,
    no_raw: NoRawOption = False,
    filter: FilterOption = None,
    filter_file: FilterFileOption = None,
) -> None:
    framework = _framework_from_context(ctx)
    tape_store = framework.get_tape_store()
    if tape_store is None:
        raise typer.BadParameter("No tape store is configured in the current Bub runtime.")

    operator_config = _operator_config(config or [])
    operator = opendal.Operator(scheme, **operator_config)
    entry_filter = EntryFilter(_filter_expressions(filter or [], filter_file or []))
    layout = ExportLayout(
        root=root,
        include_segments=not no_segments,
        include_raw_tapes=not no_raw,
    )
    report = _export_from_store(tape_store, operator, layout=layout, entry_filter=entry_filter)
    typer.echo(_render_report(report))


def _framework_from_context(ctx: typer.Context) -> BubFramework:
    framework = ctx.obj
    if not isinstance(framework, BubFramework):
        raise typer.BadParameter("This command must run inside the Bub CLI runtime.")
    return framework


def _operator_config(items: Sequence[str]) -> dict[str, str]:
    config: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(f"Invalid --config value '{item}', expected key=value.")
        key, value = item.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            raise typer.BadParameter(f"Invalid --config value '{item}', empty key.")
        config[normalized_key] = value.strip()
    return config


def _export_from_store(
    store: TapeStore | AsyncTapeStore,
    operator: opendal.Operator,
    *,
    layout: ExportLayout,
    entry_filter: EntryFilter,
) -> ExportReport:
    if is_async_tape_store(store):
        return asyncio.run(export_dataset_async(store, operator, layout=layout, entry_filter=entry_filter))
    return export_dataset(store, operator, layout=layout, entry_filter=entry_filter)


def _render_report(report: ExportReport) -> str:
    return json.dumps(
        {
            "exported_at": report.exported_at,
            "root": report.root,
            "tape_count": report.tape_count,
            "entry_count": report.entry_count,
            "segment_count": report.segment_count,
            "manifest_path": report.manifest_path,
            "files": list(report.files),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _filter_expressions(expressions: Sequence[str], files: Sequence[str]) -> list[str]:
    result = [expression.strip() for expression in expressions if expression.strip()]
    for file_name in files:
        try:
            content = open(file_name, encoding="utf-8").read().splitlines()
        except OSError as exc:
            raise typer.BadParameter(f"Failed to read filter file '{file_name}': {exc}") from exc
        for line in content:
            expression = line.strip()
            if expression and not expression.startswith("#"):
                result.append(expression)
    return result
