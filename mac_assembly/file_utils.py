"""Shared file-selection helpers (single source of truth, R3).

``newest_file`` is THE "pick the freshest artifact" rule for the whole
pipeline: part_generator (PartResult paths), the generated assembly
script's ``_resolve_part_step``, urdf_export's link STL lookup and
handoff's manifest all delegate here so a remodel run can never leave
them disagreeing about which file is current.
"""

from __future__ import annotations

import re
from pathlib import Path

_ITER_RE = re.compile(r"_(\d+)$")


def newest_file(
    directory: Path,
    pattern: str,
    *,
    require_nonempty: bool = True,
) -> Path | None:
    """Newest file matching ``pattern`` under ``directory``.

    Ordering key: ``(st_mtime_ns, trailing iteration number)``. mtime is
    primary: a fresh re-run writes new files (newer mtime) even at iter=0,
    while stale files from prior runs (higher iter numbers but older
    mtime) must NOT shadow them. The trailing ``_N`` number only breaks
    ties between files written in the same nanosecond. Lexicographic
    sorting is NEVER correct here ("temp_output_10" < "temp_output_2").

    ``require_nonempty`` skips zero-byte artifacts (a crashed writer can
    leave one behind; pointing QA / handoff at it is worse than falling
    back to the previous good file).
    """
    candidates = [
        p
        for p in directory.glob(pattern)
        if p.is_file() and (p.stat().st_size > 0 or not require_nonempty)
    ]
    if not candidates:
        return None

    def sort_key(p: Path) -> tuple[int, int]:
        m = _ITER_RE.search(p.stem)
        return (p.stat().st_mtime_ns, int(m.group(1)) if m else -1)

    return max(candidates, key=sort_key)
