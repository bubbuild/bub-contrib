"""Tape types resolved across bub versions.

bub >= 0.3.10 vendors the tape module as ``bub.tape`` and constructs entries
from it; older bub sources tapes from ``republic``. Import tape types from
this module so the whole package agrees on a single resolution order.
"""

from __future__ import annotations

try:
    from bub.tape import AsyncTapeStore, TapeEntry, TapeQuery, TapeStore
except ImportError:
    from republic import TapeEntry, TapeQuery
    from republic.tape import AsyncTapeStore, TapeStore

__all__ = ["AsyncTapeStore", "TapeEntry", "TapeQuery", "TapeStore"]
