#!/usr/bin/env python3
"""Memoised, atomic JSON I/O for the pipeline's hot readers (F1).

Why
---
The pipeline re-reads the same few multi-megabyte JSON files from several
scripts in one job: ``_model_quality_log.json`` (~7 MB) is parsed by
``_peak_deviation_stats``, ``_edge_enhancer``, ``_pnl_tracker``,
``_compute_market_edge`` and the tracker itself. Each parse is a full
``json.loads`` over the whole document, and several call sites parse the same
file more than once per process.

This module adds a stat-keyed process-level cache:

    cache key    = (resolved path, st_mtime_ns, st_size)
    cache value  = the parsed object *and* the stat that produced it

so a second read of an unchanged file is a dict lookup instead of a re-parse,
and an externally rewritten file (new mtime or size) transparently misses the
cache and re-parses. Nothing about the on-disk schema changes: this is an I/O
optimisation only, and the parsed object is exactly what ``json.load`` returns.

Public API
----------
``load(path)``
    Parsed JSON (memoised). Raises the same exceptions as ``json.load`` —
    ``JSONDecodeError`` and ``OSError`` — so existing ``except`` clauses keep
    working unchanged.

``loads_cached(text, key=...)``
    Parse JSON *text* that is already in memory, memoised on
    ``(key, len(text), hash(text))``. Useful when a caller has the bytes but
    parses them repeatedly.

``dump_compact(path, obj)``
    Atomic write with whitespace-free separators. Returns ``True`` when the
    on-disk bytes actually changed, ``False`` for a no-op — so a writer can
    skip a commit when nothing moved.

``write_if_changed(path, data)``
    Byte-level "changed?" detection for callers that already serialised.

Design notes
------------
* The cache is intentionally process-local and unbounded-by-file-count: the
  pipeline touches ~10 files, so there is nothing to evict.
* Writes are atomic (temp file in the same directory + ``os.replace``), so a
  killed run can never leave a half-written JSON document behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "clear_cache",
    "dump_compact",
    "load",
    "loads_cached",
    "write_if_changed",
]

_CACHE: dict[str, tuple[tuple[int, int], Any]] = {}
_TEXT_CACHE: dict[tuple[str, int, int], Any] = {}


def _stat_key(path: Path) -> tuple[int, int]:
    info = path.stat()
    return (info.st_mtime_ns, info.st_size)


def clear_cache() -> None:
    """Drop both caches (used by tests and by writers that just rewrote a file)."""
    _CACHE.clear()
    _TEXT_CACHE.clear()


def load(path: str | os.PathLike[str], *, use_cache: bool = True) -> Any:
    """Return the parsed JSON document at ``path``, memoised on (path, mtime, size).

    Raises ``json.JSONDecodeError`` / ``OSError`` exactly like ``json.load``.
    """
    resolved = Path(path)
    key = str(resolved.resolve())

    stat_key = _stat_key(resolved)
    if use_cache:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == stat_key:
            return cached[1]

    data = json.loads(resolved.read_text(encoding="utf-8"))
    if use_cache:
        _CACHE[key] = (stat_key, data)
    return data


def loads_cached(text: str, *, key: str = "<memory>") -> Any:
    """Parse in-memory JSON ``text``, memoised on (key, length, hash)."""
    cache_key = (key, len(text), hash(text))
    cached = _TEXT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    data = json.loads(text)
    _TEXT_CACHE[cache_key] = data
    return data


def _serialise_compact(obj: Any) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def write_if_changed(path: str | os.PathLike[str], data: bytes) -> bool:
    """Atomically write ``data`` unless the file already holds exactly these bytes.

    Returns ``True`` when the file was (re)written, ``False`` for a no-op.
    """
    target = Path(path)
    try:
        if target.exists() and target.read_bytes() == data:
            return False
    except OSError:
        pass

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # The file we just wrote supersedes anything cached for this path.
    _CACHE.pop(str(target.resolve()), None)
    return True


def dump_compact(path: str | os.PathLike[str], obj: Any) -> bool:
    """Atomically write ``obj`` as compact JSON. Returns True when bytes changed."""
    return write_if_changed(path, _serialise_compact(obj))
