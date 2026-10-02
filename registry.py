#!/usr/bin/env python3
"""Static stop ID registry.

``registry/stops.csv`` pins the merged-JDF stop number ``N`` (published as
``jdf:stop:N``) to a stop identity, so IDs survive between exports. Rows are
append-only: a rename adds an alias row with the same ``id``, a retired stop
keeps its row, and an ``id`` is never reused. ``registry/posts.csv`` pins post
suffixes (``post:<num>``, ``est:<k>``) to reference coordinates.

JrUtil gives stops that the registry does not know a provisional ID at or above
``PROVISIONAL_BASE`` and lists them in ``stop_registry_candidates.csv``. After
review, ``promote`` moves those stops into the registry.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

PROVISIONAL_BASE = 1_000_000_000
# Same-key stops must be this far apart; JrUtil's reconciler merges identical
# names whose precise coordinates are closer.
DISTINCT_STOP_METRES = 75.0
STOP_FIELDS = [
    "id",
    "town",
    "district",
    "nearby_place",
    "okres",
    "country",
    "lat",
    "lon",
    "status",
    "note",
]
POST_FIELDS = ["stop_id", "post_key", "lat", "lon", "status", "note"]
STATUSES = {"active", "retired"}
POST_KEY = re.compile(r"post:[^:\s]+|est:[1-9][0-9]*")
MERGED_INTO = re.compile(r"merged_into:([1-9][0-9]*)")


def normalize_component(value: str) -> str:
    return " ".join(value.replace(" ", " ").split()).casefold()


def stop_key(row: dict[str, str]) -> tuple[str, ...]:
    return (
        normalize_component(row["town"]),
        normalize_component(row["district"]),
        normalize_component(row["nearby_place"]),
        row["okres"].strip().upper(),
        row["country"].strip().upper(),
    )


def sort_key(row: dict[str, str]) -> tuple:
    return (int(row["id"]), stop_key(row), row["lat"], row["lon"])


def coordinate_distance_metres(
    latitude: float, longitude: float, other_latitude: float, other_longitude: float
) -> float:
    middle_latitude = math.radians((latitude + other_latitude) / 2)
    north = (latitude - other_latitude) * 111_320
    east = (longitude - other_longitude) * 111_320 * math.cos(middle_latitude)
    return math.hypot(north, east)


def _point(row: dict[str, str]) -> tuple[float, float] | None:
    if not row["lat"].strip() and not row["lon"].strip():
        return None
    return float(row["lat"]), float(row["lon"])


def read_table(path: Path, fields: list[str]) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != fields:
            raise ValueError(f"{path} must have the header {','.join(fields)}")
        return [{key: value or "" for key, value in row.items()} for row in reader]


def write_table(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def validate(stops: list[dict[str, str]], posts: list[dict[str, str]]) -> list[str]:
    errors = []
    ids = set()
    for line, row in enumerate(stops, start=2):
        where = f"stops.csv:{line}"
        try:
            stop_id = int(row["id"])
        except ValueError:
            errors.append(f"{where}: id {row['id']!r} is not an integer")
            continue
        if not 0 < stop_id < PROVISIONAL_BASE:
            errors.append(f"{where}: id {stop_id} is outside 1..{PROVISIONAL_BASE - 1}")
        if not row["town"].strip():
            errors.append(f"{where}: town is required")
        if row["status"] not in STATUSES:
            errors.append(f"{where}: status must be one of {sorted(STATUSES)}")
        try:
            point = _point(row)
            if point and not (-90 <= point[0] <= 90 and -180 <= point[1] <= 180):
                errors.append(f"{where}: coordinates out of range")
        except ValueError:
            errors.append(f"{where}: lat/lon must both be numbers or both empty")
        ids.add(stop_id)
    if errors:
        return errors

    for line, row in enumerate(stops, start=2):
        merged = MERGED_INTO.search(row["note"])
        if merged and (int(merged.group(1)) not in ids or merged.group(1) == row["id"]):
            errors.append(f"stops.csv:{line}: {merged.group(0)} must name another id")

    if [sort_key(row) for row in stops] != sorted(sort_key(row) for row in stops):
        errors.append("stops.csv must be sorted by id, then by identity")

    by_key: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in stops:
        by_key[stop_key(row)].append(row)
    for key, rows in sorted(by_key.items()):
        distinct = {}
        for row in rows:
            distinct.setdefault(row["id"], row)
        if len(distinct) < 2:
            continue
        rows = list(distinct.values())
        points = [_point(row) for row in rows]
        if any(point is None for point in points):
            errors.append(
                f"{','.join(key)} maps to ids {', '.join(sorted(distinct))} "
                "without reference coordinates for every id"
            )
            continue
        for index, left in enumerate(points):
            for other, right in enumerate(points[index + 1 :], start=index + 1):
                if coordinate_distance_metres(*left, *right) < DISTINCT_STOP_METRES:
                    errors.append(
                        f"{','.join(key)} ids {rows[index]['id']} and "
                        f"{rows[other]['id']} are closer than {DISTINCT_STOP_METRES:g} m"
                    )

    seen_posts = set()
    for line, row in enumerate(posts, start=2):
        where = f"posts.csv:{line}"
        if not row["stop_id"].isdigit() or int(row["stop_id"]) not in ids:
            errors.append(f"{where}: stop_id {row['stop_id']!r} is not registered")
        if not POST_KEY.fullmatch(row["post_key"]):
            errors.append(f"{where}: post_key must be post:<num> or est:<k>")
        if (row["stop_id"], row["post_key"]) in seen_posts:
            errors.append(f"{where}: duplicate {row['stop_id']} {row['post_key']}")
        seen_posts.add((row["stop_id"], row["post_key"]))
        if row["status"] not in STATUSES:
            errors.append(f"{where}: status must be one of {sorted(STATUSES)}")
        try:
            point = _point(row)
        except ValueError:
            point = None
            errors.append(f"{where}: lat/lon must both be numbers or both empty")
        if row["post_key"].startswith("est:") and point is None:
            errors.append(f"{where}: inferred posts need reference coordinates")
    post_order = [(int(row["stop_id"] or 0), row["post_key"]) for row in posts]
    if all(row["stop_id"].isdigit() for row in posts) and post_order != sorted(post_order):
        errors.append("posts.csv must be sorted by stop_id, then post_key")
    return errors


def _load(registry: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    return (
        read_table(registry / "stops.csv", STOP_FIELDS),
        read_table(registry / "posts.csv", POST_FIELDS),
    )


def _fail_on_errors(errors: list[str]) -> None:
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise SystemExit(1)


def run_validate(args) -> None:
    stops, posts = _load(args.registry)
    _fail_on_errors(validate(stops, posts))
    print(
        json.dumps(
            {
                "stop_rows": len(stops),
                "stop_ids": len({row["id"] for row in stops}),
                "posts": len(posts),
            },
            sort_keys=True,
        )
    )


def run_promote(args) -> None:
    """Add reviewed rows from a JrUtil candidates CSV.

    A reviewer fills ``decision`` with ``new`` (allocate the next id) or
    ``alias`` (add the identity to the existing id in ``alias_of``). Rows with
    any other decision stay out of the registry and are left for a later run.
    """
    stops, posts = _load(args.registry)
    _fail_on_errors(validate(stops, posts))
    with args.candidates.open(encoding="utf-8-sig", newline="") as stream:
        candidates = list(csv.DictReader(stream))
    known_keys = {stop_key(row): row["id"] for row in stops}
    ids = {row["id"] for row in stops}
    next_id = max((int(row["id"]) for row in stops), default=0) + 1
    added, aliased, skipped = 0, 0, 0
    for candidate in sorted(candidates, key=lambda row: stop_key(_as_stop(row, ""))):
        decision = (candidate.get("decision") or "").strip()
        if decision not in {"new", "alias"}:
            skipped += 1
            continue
        if decision == "alias":
            target = (candidate.get("alias_of") or "").strip()
            if target not in ids:
                raise ValueError(f"alias_of {target!r} is not a registered id")
            row = _as_stop(candidate, target)
        else:
            row = _as_stop(candidate, str(next_id))
        existing = known_keys.get(stop_key(row))
        if existing is not None and (decision == "alias" or _point(row) is None):
            if existing != row["id"] and decision == "alias":
                raise ValueError(
                    f"{','.join(stop_key(row))} is already registered as {existing}"
                )
            skipped += 1
            continue
        stops.append(row)
        known_keys.setdefault(stop_key(row), row["id"])
        if decision == "new":
            ids.add(row["id"])
            next_id += 1
            added += 1
        else:
            aliased += 1
    stops.sort(key=sort_key)
    _fail_on_errors(validate(stops, posts))
    write_table(args.registry / "stops.csv", STOP_FIELDS, stops)
    print(
        json.dumps(
            {"added": added, "aliased": aliased, "skipped": skipped}, sort_keys=True
        )
    )


def _as_stop(candidate: dict[str, str], stop_id: str) -> dict[str, str]:
    return {
        "id": stop_id,
        "town": (candidate.get("town") or "").strip(),
        "district": (candidate.get("district") or "").strip(),
        "nearby_place": (candidate.get("nearby_place") or "").strip(),
        "okres": (candidate.get("okres") or "").strip(),
        "country": (candidate.get("country") or "").strip(),
        "lat": (candidate.get("lat") or "").strip(),
        "lon": (candidate.get("lon") or "").strip(),
        "status": "active",
        "note": (candidate.get("note") or "").strip(),
    }


def read_merged_jdf_stops(path: Path) -> dict[str, dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        rows = csv.reader(archive.read("Zastavky.txt").decode("cp1250").splitlines())
        return {
            row[0]: {
                "id": row[0],
                "town": row[1].strip(),
                "district": row[2].strip(),
                "nearby_place": row[3].strip(),
                "okres": row[4].strip(),
                "country": row[5].strip(),
                "lat": "",
                "lon": "",
                "status": "active",
                "note": "",
            }
            for row in rows
            if row
        }


def gtfs_stop_place_points(gtfs: Path) -> dict[str, tuple[float, float]]:
    points = {}
    with (gtfs / "stops.txt").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            match = re.fullmatch(r"jdf:stop:(\d+)", row["stop_id"])
            if not match:
                continue
            latitude, longitude = float(row["stop_lat"]), float(row["stop_lon"])
            if latitude or longitude:
                points[match.group(1)] = (latitude, longitude)
    return points


def run_seed(args) -> None:
    """Bootstrap the registry from one export, keeping its current numbers.

    Reference coordinates come from the GTFS stop places. When the bundle
    metadata is available, only ``stop``-precision coordinates are kept, so
    route-derived estimates never become identity evidence.
    """
    existing, posts = _load(args.registry)
    if existing and not args.force:
        raise SystemExit("registry/stops.csv already has rows; refusing to reseed")
    stops = read_merged_jdf_stops(args.jdf)
    if args.gtfs:
        points = gtfs_stop_place_points(args.gtfs)
        precisions = None
        metadata = args.metadata or args.gtfs.parent / "source_stop_metadata.parquet"
        if args.metadata or metadata.exists():
            import pyarrow.parquet as parquet

            precisions = {
                row["gtfs_stop_id"]: row["coordinate_precision"]
                for row in parquet.read_table(metadata).to_pylist()
            }
        for stop_id, (latitude, longitude) in points.items():
            if stop_id not in stops:
                continue
            if precisions is not None and precisions.get(f"jdf:stop:{stop_id}") != "stop":
                continue
            stops[stop_id]["lat"] = f"{latitude:.6f}"
            stops[stop_id]["lon"] = f"{longitude:.6f}"
    rows = sorted(stops.values(), key=sort_key)
    _fail_on_errors(validate(rows, posts))
    write_table(args.registry / "stops.csv", STOP_FIELDS, rows)
    print(json.dumps({"seeded": len(rows)}, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root.add_argument("--registry", type=Path, default=Path(__file__).parent / "registry")
    commands = root.add_subparsers(required=True)
    command = commands.add_parser("validate")
    command.set_defaults(handler=run_validate)
    command = commands.add_parser("promote")
    command.add_argument("candidates", type=Path)
    command.set_defaults(handler=run_promote)
    command = commands.add_parser("seed")
    command.add_argument("jdf", type=Path, help="merged JDF zip of the export to keep")
    command.add_argument("--gtfs", type=Path, help="GTFS directory for reference coordinates")
    command.add_argument(
        "--metadata",
        type=Path,
        help="source_stop_metadata.parquet (defaults to the parent of the GTFS directory)",
    )
    command.add_argument("--force", action="store_true", help="replace existing rows")
    command.set_defaults(handler=run_seed)
    return root


def main() -> None:
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
