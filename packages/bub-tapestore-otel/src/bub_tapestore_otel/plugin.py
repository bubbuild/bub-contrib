from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

from pydantic import Field
from pydantic_settings import SettingsConfigDict

import bub
from bub import BubFramework, hookimpl
from bub_tapestore_otel.exporter import LogfireTapeExporter, LogfireTapeExporterSettings
from bub_tapestore_otel.store import OTelTapeStore

CONFIG_NAME = "tapestore-otel"


@bub.config(name=CONFIG_NAME)
class OTelTapeStoreSettings(bub.Settings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    enabled: bool = Field(default=True, validation_alias="BUB_TAPESTORE_OTEL_ENABLED")
    service_name: str = Field(default="bub", validation_alias="BUB_TAPESTORE_OTEL_SERVICE_NAME")
    send_to_logfire: bool = Field(default=False, validation_alias="BUB_TAPESTORE_OTEL_SEND_TO_LOGFIRE")
    force_flush: bool = Field(default=True, validation_alias="BUB_TAPESTORE_OTEL_FORCE_FLUSH")
    shutdown_after_flush: bool = Field(default=True, validation_alias="BUB_TAPESTORE_OTEL_SHUTDOWN_AFTER_FLUSH")


class OTelTapeStorePlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework

    @hookimpl(tryfirst=True)
    def provide_tape_store(self) -> Any:
        parent = self.framework._plugin_manager.subset_hook_caller(
            "provide_tape_store",
            remove_plugins=[self],
        )
        store = parent()
        settings = bub.ensure_config(OTelTapeStoreSettings)
        if not settings.enabled:
            return store
        exporter = LogfireTapeExporter(
            LogfireTapeExporterSettings(
                service_name=settings.service_name,
                send_to_logfire=settings.send_to_logfire,
                force_flush=settings.force_flush,
                shutdown_after_flush=settings.shutdown_after_flush,
            )
        )
        return _wrap_store_result(store, exporter)


def _wrap_store_result(store: Any, exporter: LogfireTapeExporter) -> Any:
    if isinstance(store, AsyncIterator):

        @contextlib.asynccontextmanager
        async def manager() -> AsyncIterator[OTelTapeStore]:
            async with contextlib.asynccontextmanager(lambda: store)() as inner:
                yield OTelTapeStore(inner, exporter)

        return manager()

    if isinstance(store, Iterator):

        @contextlib.contextmanager
        def manager() -> Iterator[OTelTapeStore]:
            with contextlib.contextmanager(lambda: store)() as inner:
                yield OTelTapeStore(inner, exporter)

        return manager()

    return OTelTapeStore(store, exporter)
