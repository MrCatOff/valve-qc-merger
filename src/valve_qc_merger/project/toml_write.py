"""Minimal TOML writer for project files (stdlib ``tomllib`` only reads).

Supports exactly what ``project.toml`` needs: top-level scalars, tables,
arrays of tables, and inside them strings, bools, ints, floats, lists of
scalars and nested tables. ``None`` values are omitted (TOML has no null).
"""

from __future__ import annotations

import json
import math
from typing import Any


def _key(key: str) -> str:
    if key and all(ch.isalnum() or ch in "-_" for ch in key):
        return key
    return _string(key)


def _string(value: str) -> str:
    # A JSON string is a valid TOML basic string (\" \\ \n \t \uXXXX escapes).
    return json.dumps(value, ensure_ascii=False)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("TOML cannot hold NaN/inf in project files")
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    raise TypeError(f"unsupported TOML value: {value!r}")


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _emit_table(lines: list[str], path: str, table: dict[str, Any]) -> None:
    scalars = {k: v for k, v in table.items()
               if v is not None and not isinstance(v, dict) and not _is_table_array(v)}
    for key, value in scalars.items():
        lines.append(f"{_key(key)} = {_scalar(value)}")
    for key, value in table.items():
        if isinstance(value, dict):
            sub = f"{path}.{_key(key)}" if path else _key(key)
            lines.append("")
            lines.append(f"[{sub}]")
            _emit_table(lines, sub, value)
    for key, value in table.items():
        if _is_table_array(value):
            sub = f"{path}.{_key(key)}" if path else _key(key)
            for item in value:
                lines.append("")
                lines.append(f"[[{sub}]]")
                _emit_table(lines, sub, item)


def dumps(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _emit_table(lines, "", data)
    return "\n".join(lines).lstrip("\n") + "\n"


__all__ = ["dumps"]
