#!/usr/bin/env python3
"""Reviewed JDF route rules.

``routes/transport-modes.csv`` corrects the JDF transport mode of reviewed CIS
lines (for example city trams exported as buses). ``routes/presentation.csv``
overrides the published line marking and colours. JrUtil applies both
(``--transport-mode-rules`` and ``--route-presentation-rules``); this module
validates them with the same licence pattern grammar:

- ``915003``: one licence
- ``915001-915019``: an inclusive range of equally long licences
- ``915*``: a licence prefix; ``*`` alone matches every licence

The most specific rule wins: a single licence, then a range (narrower first),
then a prefix (longer first). At equal specificity a rule naming an agency wins
over one without. Two rules of equal specificity that could set the same field
of one route are an error.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "routes"
MODE_FIELDS = ["agency_id", "licence", "public_line", "expected_mode", "effective_mode", "reason"]
PRESENTATION_FIELDS = [
    "agency_id",
    "licence",
    "route_short_name",
    "route_color",
    "route_text_color",
    "reason",
]
PRESENTED_FIELDS = ("route_short_name", "route_color", "route_text_color")
MODES = frozenset("AETLMP")
_COLOR = re.compile(r"[0-9A-Fa-f]{6}")
_AGENCY = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class LicencePattern:
    low: str
    high: str
    kind: int  # 3 licence, 2 range, 1 prefix
    narrowness: int

    def overlaps(self, other: LicencePattern) -> bool:
        if self.kind == 1 or other.kind == 1:
            return _prefix_overlaps(self, other)
        return len(self.low) == len(other.low) and self.low <= other.high and other.low <= self.high


def _prefix_overlaps(left: LicencePattern, right: LicencePattern) -> bool:
    if left.kind != 1:
        left, right = right, left
    prefix = left.low
    if right.kind == 1:
        return right.low.startswith(prefix) or prefix.startswith(right.low)
    width = len(right.low)
    if len(prefix) > width:
        return False
    lowest = prefix.ljust(width, "0")
    highest = prefix.ljust(width, "9")
    return lowest <= right.high and right.low <= highest


def parse_licence(value: str) -> LicencePattern:
    value = value.strip()
    if value.endswith("*"):
        prefix = value[:-1]
        if prefix and not prefix.isdigit():
            raise ValueError(f"invalid licence prefix: {value}")
        return LicencePattern(prefix, prefix, 1, len(prefix))
    if "-" in value:
        low, _, high = value.partition("-")
        if not (low.isdigit() and high.isdigit() and len(low) == len(high) and low <= high):
            raise ValueError(f"invalid licence range: {value}")
        return LicencePattern(low, high, 2, -(int(high) - int(low)))
    if not value.isdigit():
        raise ValueError(f"invalid licence: {value}")
    return LicencePattern(value, value, 3, 0)


def _public_line_guard(value: str) -> tuple[int, int] | None:
    if not value:
        return None
    low, _, high = value.partition("-")
    high = high or low
    if not (low.isdigit() and high.isdigit() and int(low) <= int(high)):
        raise ValueError(f"invalid public_line guard: {value}")
    return int(low), int(high)


def _guards_disjoint(left: dict[str, str], right: dict[str, str]) -> bool:
    if left["expected_mode"] != right["expected_mode"]:
        return True
    try:
        left_guard = _public_line_guard(left["public_line"])
        right_guard = _public_line_guard(right["public_line"])
    except ValueError:
        return True
    if left_guard is None or right_guard is None:
        return False
    return left_guard[1] < right_guard[0] or right_guard[1] < left_guard[0]


def read_table(path: Path, fields: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != fields:
            raise ValueError(f"{path.name} must have header {','.join(fields)}")
        return [{key: (value or "").strip() for key, value in row.items()} for row in reader]


def _common_errors(label: str, row: dict[str, str]) -> tuple[LicencePattern | None, list[str]]:
    errors = []
    pattern = None
    try:
        pattern = parse_licence(row["licence"])
    except ValueError as error:
        errors.append(f"{label}: {error}")
    if row["agency_id"] and not _AGENCY.fullmatch(row["agency_id"]):
        errors.append(f"{label}: agency_id must be an IČO: {row['agency_id']}")
    if not row["reason"]:
        errors.append(f"{label}: reason is required")
    return pattern, errors


def _specificity(pattern: LicencePattern, row: dict[str, str]) -> tuple[int, int, bool]:
    return pattern.kind, pattern.narrowness, bool(row["agency_id"])


def _conflicts(
    name: str,
    rules: list[tuple[int, LicencePattern, dict[str, str]]],
    fields: tuple[str, ...],
    disjoint=lambda left, right: False,
) -> list[str]:
    errors = []
    for (left_line, left, left_row), (right_line, right, right_row) in combinations(rules, 2):
        if _specificity(left, left_row) != _specificity(right, right_row):
            continue
        if left_row["agency_id"] != right_row["agency_id"] or not left.overlaps(right):
            continue
        if disjoint(left_row, right_row):
            continue
        shared = [field for field in fields if left_row[field] and right_row[field]]
        if shared:
            errors.append(
                f"{name}:{left_line} and {name}:{right_line} equally specific for "
                f"overlapping licences and both set {','.join(shared)}"
            )
    return errors


def validate_modes(rows: list[dict[str, str]], name: str = "transport-modes.csv") -> list[str]:
    errors: list[str] = []
    rules = []
    for line, row in enumerate(rows, start=2):
        label = f"{name}:{line}"
        pattern, row_errors = _common_errors(label, row)
        errors += row_errors
        try:
            _public_line_guard(row["public_line"])
        except ValueError as error:
            errors.append(f"{label}: {error}")
        for field in ("expected_mode", "effective_mode"):
            if row[field] not in MODES:
                errors.append(f"{label}: unknown {field}: {row[field]}")
        if row["expected_mode"] == row["effective_mode"]:
            errors.append(f"{label}: effective_mode equals expected_mode")
        if pattern is not None:
            rules.append((line, pattern, {**row, "mode": "x"}))
    return errors + _conflicts(name, rules, ("mode",), _guards_disjoint)


def validate_presentation(rows: list[dict[str, str]], name: str = "presentation.csv") -> list[str]:
    errors: list[str] = []
    rules = []
    for line, row in enumerate(rows, start=2):
        label = f"{name}:{line}"
        pattern, row_errors = _common_errors(label, row)
        errors += row_errors
        if not any(row[field] for field in PRESENTED_FIELDS):
            errors.append(f"{label}: rule overrides nothing")
        for field in ("route_color", "route_text_color"):
            if row[field] and not _COLOR.fullmatch(row[field]):
                errors.append(f"{label}: {field} must be six hex digits: {row[field]}")
        if pattern is not None:
            rules.append((line, pattern, row))
    return errors + _conflicts(name, rules, PRESENTED_FIELDS)


def validate_directory(root: Path) -> list[str]:
    errors = []
    for name, fields, check in (
        ("transport-modes.csv", MODE_FIELDS, validate_modes),
        ("presentation.csv", PRESENTATION_FIELDS, validate_presentation),
    ):
        try:
            errors += check(read_table(root / name, fields), name)
        except (OSError, ValueError) as error:
            errors.append(str(error))
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT)
    errors = validate_directory(parser.parse_args().directory)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        raise SystemExit(1)
    print("Route rules are valid")


if __name__ == "__main__":
    main()
