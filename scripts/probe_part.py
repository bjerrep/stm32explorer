#!/usr/bin/env python3
"""Probe a single part in an ST selector raw-data file.

Usage:
    python3 probe_part.py <raw_data_file> <part_number>

The raw file is the ST product-selector JSON (the `raw_selector_data.json`
shape: a {"columns":[...], "rows":[...]} table where each row carries a list
of {"columnId","value"} cells). This dumps the matched row's raw JSON and a
human-readable digest that resolves every populated cell to its column name.
"""
import json
import sys


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def col_name_map(data):
    """columnId -> column name, from the table's `columns` list."""
    out = {}
    for c in data.get("columns", []):
        cid = str(c.get("id", ""))
        out[cid] = c.get("name") or c.get("description") or cid
    return out


def part_number(row):
    """A row's part number lives in the cell with columnId '1'."""
    for cell in row.get("cells", []):
        if str(cell.get("columnId")) == "1":
            return str(cell.get("value", ""))
    return ""


def find_rows(data, query):
    """Match rows by part number: exact (case-insensitive) first, else substring."""
    rows = data.get("rows", [])
    q = query.strip().upper()
    exact = [r for r in rows if part_number(r).upper() == q]
    if exact:
        return exact, "exact"
    partial = [r for r in rows if q in part_number(r).upper()]
    return partial, "substring"


def digest(row, names):
    """Readable 'Column Name (id): value' lines for every non-empty cell."""
    lines = []
    for cell in row.get("cells", []):
        cid = str(cell.get("columnId"))
        val = cell.get("value")
        if val is None or str(val).strip() == "":
            continue
        lines.append((names.get(cid, f"col#{cid}"), cid, val))
    # part number / description first, then alphabetical by column name
    lead = {"1": 0, "4": 1}
    lines.sort(key=lambda t: (lead.get(t[1], 2), t[0].lower()))
    return lines


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    raw_path, query = argv[1], argv[2]
    data = load(raw_path)
    names = col_name_map(data)

    matches, how = find_rows(data, query)
    if not matches:
        print(f"No part matching '{query}' in {raw_path}", file=sys.stderr)
        return 1

    if how == "substring" and len(matches) > 1:
        print(f"'{query}' is ambiguous — {len(matches)} substring matches:", file=sys.stderr)
        for r in matches:
            print(f"  {part_number(r)}", file=sys.stderr)
        print("Re-run with a full part number.", file=sys.stderr)
        return 1

    row = matches[0]
    pn = part_number(row)

    print("=" * 72)
    print(f"RAW JSON  —  {pn}  ({how} match)")
    print("=" * 72)
    print(json.dumps(row, indent=2, ensure_ascii=False))

    lines = digest(row, names)
    print()
    print("=" * 72)
    print(f"DIGEST  —  {pn}  —  {len(lines)} parameters found")
    print("=" * 72)
    width = max((len(n) for n, _, _ in lines), default=0)
    for name, cid, val in lines:
        print(f"  {name:<{width}}  [{cid}]  {val}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
