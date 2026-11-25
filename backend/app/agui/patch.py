"""Minimal RFC 6902 JSON Patch: `diff` between two JSON documents and `apply`.

Enough for the officer's run view (dicts, lists that mostly grow, scalars), so no extra
dependency: lists are compared element by element, extra items are appended, missing ones
removed from the end.
"""

import copy
from typing import Any

Op = dict[str, Any]


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def diff(old: Any, new: Any, path: str = "") -> list[Op]:
    if isinstance(old, dict) and isinstance(new, dict):
        ops: list[Op] = []
        for key in old:
            if key not in new:
                ops.append({"op": "remove", "path": f"{path}/{escape(key)}"})
        for key, value in new.items():
            child = f"{path}/{escape(key)}"
            if key not in old:
                ops.append({"op": "add", "path": child, "value": value})
            else:
                ops.extend(diff(old[key], value, child))
        return ops
    if isinstance(old, list) and isinstance(new, list):
        ops = []
        common = min(len(old), len(new))
        for i in range(common):
            ops.extend(diff(old[i], new[i], f"{path}/{i}"))
        for value in new[common:]:
            ops.append({"op": "add", "path": f"{path}/-", "value": value})
        for i in range(len(old) - 1, common - 1, -1):
            ops.append({"op": "remove", "path": f"{path}/{i}"})
        return ops
    if old == new and type(old) is type(new):
        return []
    return [{"op": "replace", "path": path or "", "value": new}]


def apply(doc: Any, ops: list[Op]) -> Any:
    """Apply add / remove / replace ops (what `diff` produces) to a copy of `doc`."""
    doc = copy.deepcopy(doc)
    for op in ops:
        if op["path"] == "":
            doc = copy.deepcopy(op["value"])
            continue
        *parents, last = [_unescape(t) for t in op["path"].split("/")[1:]]
        target = doc
        for token in parents:
            target = target[int(token)] if isinstance(target, list) else target[token]
        if isinstance(target, list):
            if op["op"] == "add":
                if last == "-":
                    target.append(copy.deepcopy(op["value"]))
                else:
                    target.insert(int(last), copy.deepcopy(op["value"]))
            elif op["op"] == "remove":
                del target[int(last)]
            else:
                target[int(last)] = copy.deepcopy(op["value"])
        elif op["op"] == "remove":
            del target[last]
        else:
            target[last] = copy.deepcopy(op["value"])
    return doc
