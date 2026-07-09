from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import bub
from bub import BubFramework, hookimpl
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub_tapestore_otel.exporter import OTelTapeExporter, OTelTapeExporterSettings
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
    service_name: str = Field(
        default="bub", validation_alias="BUB_TAPESTORE_OTEL_SERVICE_NAME"
    )
    agent_name: str = Field(
        default="bub", validation_alias="BUB_TAPESTORE_OTEL_AGENT_NAME"
    )


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
        exporter = OTelTapeExporter(
            OTelTapeExporterSettings(
                service_name=settings.service_name,
                agent_name=settings.agent_name,
            )
        )
        return _wrap_store_result(store, exporter)


def _wrap_store_result(store: Any, exporter: OTelTapeExporter) -> Any:
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
