from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize("missing_anchor", [False, True])
def test_framework_process_exits_after_using_sqlite_store(
    tmp_path: Path, missing_anchor: bool
) -> None:
    script = textwrap.dedent("""\
        import asyncio
        import sys
        from pathlib import Path
        from bub.framework import BubFramework
        from bub.tape import TapeEntry, TapeQuery
        from bub_tapestore_sqlite import plugin
        from bub_tapestore_sqlite.store import SQLiteTapeStore

        framework = BubFramework(config_file=Path('config.yml'))
        framework.plugin_manager.register(plugin, name='tapestore-sqlite')

        async def main():
            async with framework.running():
                store = framework.get_tape_store()
                assert isinstance(store, SQLiteTapeStore)
                assert store is framework.get_tape_store()
                await store.append('lifecycle', TapeEntry.system('first'))
                if sys.argv[1] == 'missing-anchor':
                    await TapeQuery('lifecycle', store).after_anchor('missing').all()

            async with framework.running():
                reused = framework.get_tape_store()
                assert reused is store
                assert await reused.list_tapes() == ['lifecycle']
                await reused.append('lifecycle', TapeEntry.system('second'))
                entries = list(await TapeQuery('lifecycle', reused).all())
                assert [entry.payload['content'] for entry in entries] == ['first', 'second']

        asyncio.run(main())
        print('framework_completed', flush=True)
        """)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            "missing-anchor" if missing_anchor else "normal",
        ],
        cwd=tmp_path,
        env=os.environ
        | {
            "BUB_HOME": str(tmp_path),
            "BUB_SQLITE_PATH": str(tmp_path / "tapes.sqlite3"),
            "BUB_SQLITE_EMBEDDING_MODEL": "",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    if missing_anchor:
        assert result.returncode == 1, result.stderr
        assert "Anchor 'missing' was not found." in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "framework_completed" in result.stdout


def test_cli_process_exits_after_using_sqlite_plugin(tmp_path: Path) -> None:
    script = textwrap.dedent("""\
        from pathlib import Path
        from bub.framework import BubFramework
        from bub_tapestore_sqlite import plugin

        framework = BubFramework(config_file=Path('config.yml'))
        framework.load_builtin_hooks()
        framework.plugin_manager.register(plugin, name='tapestore-sqlite')
        framework.create_cli_app()(args=['run', ',tape.info'])
        """)
    database = tmp_path / "tapes.sqlite3"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "BUB_HOME": str(tmp_path),
            "BUB_SQLITE_PATH": str(database),
            "BUB_SQLITE_EMBEDDING_MODEL": "",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "[cli:local]" in result.stdout
    assert "entries:" in result.stdout
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tape_entries").fetchone()[0] > 0
