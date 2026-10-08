"""Generate src/pyeebus/spine/schema.py from the spine-go model.

Usage: python tools/gen_schema.py <path to spine-go/model>

Extracts, per SPINE function, the list field and key fields used for partial
updates (SPINE 5.3.4) plus the filter selector/element names.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STRUCT = re.compile(r"^type (\w+) struct \{\n(.*?)^\}", re.MULTILINE | re.DOTALL)
FIELD = re.compile(r'^\s*(\w+)\s+(\[\])?\*?([\w.]+)\s+`json:"(\w+)[^"]*"(?:\s+eebus:"([^"]*)")?`', re.MULTILINE)
UPDATER = re.compile(r"func \(r \*(\w+)\) UpdateList\(.*?newList\.\(\*\w+\)\.(\w+)", re.DOTALL)


def main(model_dir: str) -> None:
    src = "\n".join(p.read_text() for p in sorted(Path(model_dir).glob("*.go"))
                    if not p.name.endswith("_test.go"))
    structs: dict[str, list[tuple[str, bool, str, str, str]]] = {}
    for name, body in STRUCT.findall(src):
        structs[name] = [(f, bool(sl), t, j, tags or "") for f, sl, t, j, tags in FIELD.findall(body)]

    # function name -> data type (CmdType) and filter selector/element names
    functions: dict[str, str] = {}
    for _f, _sl, typ, js, tags in structs["CmdType"]:
        m = re.search(r"fct:(\w+)", tags)
        if m:
            functions[m.group(1)] = typ
    selectors: dict[str, str] = {}
    elements: dict[str, str] = {}
    for _f, _sl, _typ, js, tags in structs["FilterType"]:
        m = re.search(r"fct:(\w+)", tags)
        fct = m.group(1) if m else re.sub(r"(Selectors|Elements)$", "", js)
        if js.endswith("Selectors"):
            selectors[fct] = js
        elif js.endswith("Elements"):
            elements[fct] = js

    updaters = dict(UPDATER.findall(src))
    lists: dict[str, tuple[str, list[str], list[str]]] = {}
    for fct, typ in sorted(functions.items()):
        if typ not in updaters:
            continue
        field = next(f for f in structs[typ] if f[0] == updaters[typ])
        item = structs.get(field[2], [])  # some lists hold plain values
        keys = [j for _f, _sl, _t, j, tags in item if "key" in tags.split(",")]
        primary = [j for _f, _sl, _t, j, tags in item if "primarykey" in tags.split(",")]
        lists[fct] = (field[3], keys, primary)

    out = ['"""SPINE function metadata generated from spine-go\'s model by tools/gen_schema.py.',
           "", "Do not edit by hand.", '"""', "",
           "# function -> (list field, key fields, primary key fields)",
           "LIST_FUNCTIONS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {"]
    for fct, (field, keys, primary) in lists.items():
        out.append(f"    {fct!r}: ({field!r}, {tuple(keys)!r}, {tuple(primary)!r}),")
    out += ["}", "", "# all functions known to the data model", "FUNCTIONS: frozenset[str] = frozenset({"]
    out += [f"    {f!r}," for f in sorted(functions)]
    out += ["})", "", "SELECTORS: dict[str, str] = {"]
    out += [f"    {f!r}: {s!r}," for f, s in sorted(selectors.items())]
    out += ["}", "", "ELEMENTS: dict[str, str] = {"]
    out += [f"    {f!r}: {s!r}," for f, s in sorted(elements.items())]
    out += ["}", ""]
    target = Path(__file__).resolve().parent.parent / "src/pyeebus/spine/schema.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out))
    print(f"{len(lists)} list functions, {len(functions)} functions -> {target}")


if __name__ == "__main__":
    main(sys.argv[1])
