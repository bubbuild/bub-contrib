from __future__ import annotations

import bub_tapestore_otel.plugin as plugin
import pluggy
from bub.hooks import BUB_HOOK_NAMESPACE, BubHookSpecs, hookimpl
from bub_tapestore_otel.plugin import OTelTapeStorePlugin, OTelTapeStoreSettings
from bub_tapestore_otel.store import OTelTapeStore


class ParentStore:
    pass


class ParentPlugin:
    @hookimpl
    def provide_tape_store(self) -> ParentStore:
        return ParentStore()


class Framework:
    def __init__(self) -> None:
        self._plugin_manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
        self._plugin_manager.add_hookspecs(BubHookSpecs)


def test_plugin_wraps_parent_tape_store(monkeypatch) -> None:
    framework = Framework()
    otel_plugin = OTelTapeStorePlugin(framework)  # type: ignore[arg-type]
    framework._plugin_manager.register(ParentPlugin(), "parent")
    framework._plugin_manager.register(otel_plugin, "otel")
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: OTelTapeStoreSettings(enabled=True),
    )

    store = framework._plugin_manager.hook.provide_tape_store()

    assert isinstance(store, OTelTapeStore)
    assert isinstance(store._inner, ParentStore)


def test_plugin_can_be_disabled(monkeypatch) -> None:
    framework = Framework()
    otel_plugin = OTelTapeStorePlugin(framework)  # type: ignore[arg-type]
    framework._plugin_manager.register(ParentPlugin(), "parent")
    framework._plugin_manager.register(otel_plugin, "otel")
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: OTelTapeStoreSettings(enabled=False),
    )

    store = framework._plugin_manager.hook.provide_tape_store()

    assert isinstance(store, ParentStore)
