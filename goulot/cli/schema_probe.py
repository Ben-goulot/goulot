"""Schema variants for `goulot dev probe` (development only): which output schemas does the API compile?

A refused schema (« compiled grammar is too large », HTTP 400) is not billed; an accepted one is answered with a
64-token cap, a fraction of a cent. The variants never replace a versioned schema by themselves: a variant that
must be used becomes a new `_vN` file (CLAUDE.md, prompts and schemas).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

SchemaT = dict[str, Any]


def _walk(node: Any, fn: Callable[[dict[str, Any]], None]) -> None:
    if isinstance(node, dict):
        fn(node)
        for value in node.values():
            _walk(value, fn)
    elif isinstance(node, list):
        for value in node:
            _walk(value, fn)


def enums_to_descriptions(schema: SchemaT) -> SchemaT:
    out = copy.deepcopy(schema)

    def fix(n: dict[str, Any]) -> None:
        if "enum" in n and n.get("type") in ("string", "integer"):
            values = n.pop("enum")
            n["description"] = (str(n.get("description", "")) + " Une valeur parmi : "
                                + ", ".join(map(str, values)) + ".").strip()

    _walk(out, fix)
    return out


def without(schema: SchemaT, *props: str) -> SchemaT:
    """The schema without some top-level properties, and the definitions only they used."""
    out = copy.deepcopy(schema)
    for p in props:
        out["properties"].pop(p, None)
        if p in out.get("required", []):
            out["required"].remove(p)
    used: set[str] = set()

    def refs(n: dict[str, Any]) -> None:
        ref = n.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            used.add(ref.split("/")[-1])

    for _ in range(5):  # definitions can reference each other
        _walk(out["properties"], refs)
        for name in list(used):
            _walk(out.get("$defs", {}).get(name, {}), refs)
    if "$defs" in out:
        out["$defs"] = {k: v for k, v in out["$defs"].items() if k in used}
    return out


def only(schema: SchemaT, *props: str) -> SchemaT:
    keep = set(props) | {"schema_version", "injection_suspected"}
    return without(schema, *[p for p in schema["properties"] if p not in keep])
