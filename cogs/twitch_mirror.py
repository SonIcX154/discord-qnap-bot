from __future__ import annotations
"""Twitch chat ↔ Discord mirror (native robotty catch-up).

Source is split into utils/_tm_part1.py … _tm_part3.py for readable git diffs;
assembled here at import time.
"""
from pathlib import Path

_parts_dir = Path(__file__).resolve().parent.parent / "utils"
_src = "".join(
    (_parts_dir / f"_tm_part{i}.py").read_text(encoding="utf-8")
    for i in (1, 2, 3)
)
exec(compile(_src, str(_parts_dir / "_tm_assembled.py"), "exec"), globals())
