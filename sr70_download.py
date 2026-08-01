#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import hashlib
import io
import openpyxl
import os
from pathlib import Path
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass


def get_latest_url():
    import lxml.html
    import requests

    resp = requests.get(
        "https://provoz.spravazeleznic.cz/Portal/ViewArticle.aspx?oid=34462"
    )
    doc = lxml.html.fromstring(resp.content)
    xls_rows = doc.xpath(
        "//div[@class='innerContent']//tr[.//img[contains(@src,'XLS')] and contains(.//a/text(), 'SR70')]"
    )
    candidates = []
    for row in xls_rows:
        candidates.append(
            (
                dt.datetime.strptime(row.xpath("td[5]/text()")[0], "%d.%m.%Y %H:%M:%S"),
                row.xpath("td[1]/a/@href")[0],
            )
        )
    candidates.sort()
    return "https://provoz.spravazeleznic.cz/Portal/" + candidates[-1][1]


def parse_gps(gps):
    return float(re.sub("N|E|°", "", gps).replace(",", "."))


@dataclass(frozen=True)
class Sr70Row:
    code: str
    name: str
    name20: str
    latitude: float
    longitude: float
    qualifier: int


def _code(value):
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"non-integral SR70 identifier {value!r}")
        value = int(value)
    result = str(value).strip().rjust(6, "0")
    if len(result) != 6 or not result.isdigit():
        raise ValueError(f"invalid SR70 identifier {value!r}")
    return result


def load_rows(file):
    wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
    sh = wb.active
    header = [c.value for c in sh[1]]
    required = [
        "Evidenční číslo",
        "Název",
        "Název 20",
        "GPS N (DEG)",
        "GPS E (DEG)",
        "Kvalifikátor",
    ]
    missing = [name for name in required if name not in header]
    if missing:
        raise ValueError(f"missing required SR70 columns: {', '.join(missing)}")

    rows = []
    for rownum, values in enumerate(sh.iter_rows(min_row=2, values_only=True), start=2):
        if all(value is None for value in values):
            continue
        try:
            code = _code(values[header.index("Evidenční číslo")])
            name_value = values[header.index("Název")]
            name20_value = values[header.index("Název 20")]
            if not isinstance(name_value, str) or not isinstance(name20_value, str):
                raise ValueError("blank or non-text official name")
            name = name_value.strip()
            name20 = name20_value.strip()
            latitude = parse_gps(str(values[header.index("GPS N (DEG)")]))
            longitude = parse_gps(str(values[header.index("GPS E (DEG)")]))
            qualifier = int(values[header.index("Kvalifikátor")])
            if not name or not name20:
                raise ValueError("blank official name")
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError("coordinates outside WGS84 bounds")
            rows.append(Sr70Row(code, name, name20, latitude, longitude, qualifier))
        except Exception as e:
            raise ValueError(f"error processing SR70 row {rownum}: {e}") from e

    wb.close()
    duplicate_codes = sorted(
        code for code, count in Counter(row.code for row in rows).items() if count > 1
    )
    if duplicate_codes:
        raise ValueError(
            f"duplicate SR70 identifiers: {', '.join(duplicate_codes[:10])}"
        )
    prefixes = {}
    for row in rows:
        prefixes.setdefault(row.code[:5], []).append(row.code)
    prefix_conflicts = sorted(
        prefix for prefix, codes in prefixes.items() if len(codes) > 1
    )
    if prefix_conflicts:
        raise ValueError(
            "SR70 five-digit identity collisions: " + ", ".join(prefix_conflicts[:10])
        )
    return sorted(rows, key=lambda row: row.code)


def _format_number(value):
    return format(value, ".15g")


def _write_csv(path, rows, name):
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        for row in rows:
            writer.writerow(
                [
                    row.code,
                    name(row),
                    _format_number(row.latitude),
                    _format_number(row.longitude),
                ]
            )


def write_pair(output_dir, rows):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for filename, name in [
            ("SR70.csv", lambda row: row.name),
            ("SR70_Nazev20.csv", lambda row: row.name20),
        ]:
            fd, temporary = tempfile.mkstemp(
                prefix=f".{filename}.", suffix=".tmp", dir=output_dir
            )
            os.close(fd)
            temporary_path = Path(temporary)
            _write_csv(temporary_path, rows, name)
            staged.append((temporary_path, output_dir / filename))
        for temporary_path, destination in staged:
            os.replace(temporary_path, destination)
    finally:
        for temporary_path, _ in staged:
            temporary_path.unlink(missing_ok=True)


def process(file, name_col, just_names, only_stops):
    rows = load_rows(file)
    allowed_qualifiers = {1, 61, 23, 24, 27, 28, 33, 34, 37, 38, 43, 44, 52, 62}
    output = csv.writer(sys.stdout, lineterminator="\n")
    for row in rows:
        if only_stops and row.qualifier not in allowed_qualifiers:
            continue
        name = row.name20 if name_col == "Název 20" else row.name
        fields = [row.code, name]
        if not just_names:
            fields.extend([_format_number(row.latitude), _format_number(row.longitude)])
        output.writerow(fields)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="Název", choices=("Název", "Název 20"))
    parser.add_argument("--just-names", action="store_true")
    parser.add_argument("--only-stops", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("input", nargs="?", type=Path)
    args = parser.parse_args()

    if args.input is None:
        import requests

        url = get_latest_url()
        print(f"Downloading from {url}...", file=sys.stderr)
        resp = requests.get(url)
        resp.raise_for_status()
        infile = io.BytesIO(resp.content)
    else:
        infile = args.input

    if args.output_dir is not None:
        rows = load_rows(infile)
        write_pair(args.output_dir, rows)
        source_hash = (
            hashlib.sha256(args.input.read_bytes()).hexdigest()
            if args.input is not None
            else "downloaded"
        )
        print(
            f"Wrote {len(rows)} SR70 rows to {args.output_dir} "
            f"(source SHA-256 {source_hash})",
            file=sys.stderr,
        )
    else:
        process(infile, args.name, args.just_names, args.only_stops)


if __name__ == "__main__":
    main()
