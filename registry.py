#!/usr/bin/env python3
"""Static stop ID registry.

``registry/stops.csv`` pins the merged-JDF stop number ``N`` (published as
``jdf:stop:N``) to a stop identity, so IDs survive between exports. Rows are
append-only: a rename adds an alias row with the same ``id``, a retired stop
keeps its row, and an ``id`` is never reused. ``registry/posts.csv`` pins
inferred post ordinals (``est:<k>``) to reference coordinates, and
``registry/overlay_places.csv`` pins regional-overlay stop places that match no
national stop.

JrUtil numbers stops the registry does not know from ``PROVISIONAL_BASE`` and
writes them, new post ordinals and unpinned overlay places to candidate CSVs
(``--stop-registry-candidates``). After review, ``promote`` adds them here.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
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
PLACE_FIELDS = ["source_id", "group_key", "place_id", "status", "note"]
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


def validate(
    stops: list[dict[str, str]],
    posts: list[dict[str, str]],
    places: list[dict[str, str]] | None = None,
) -> list[str]:
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

    merged_ids = {row["id"] for row in stops if MERGED_INTO.search(row["note"])}
    for line, row in enumerate(stops, start=2):
        merged = MERGED_INTO.search(row["note"])
        if not merged:
            continue
        target = merged.group(1)
        if int(target) not in ids or target == row["id"]:
            errors.append(f"stops.csv:{line}: {merged.group(0)} must name another id")
        elif target in merged_ids:
            errors.append(f"stops.csv:{line}: {merged.group(0)} is itself merged")

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

    seen_places = set()
    for line, row in enumerate(places or [], start=2):
        where = f"overlay_places.csv:{line}"
        if not (row["source_id"].strip() and row["group_key"] and row["place_id"].strip()):
            errors.append(f"{where}: source_id, group_key and place_id are required")
        if (row["source_id"], row["group_key"]) in seen_places:
            errors.append(f"{where}: duplicate {row['source_id']} {row['group_key']}")
        seen_places.add((row["source_id"], row["group_key"]))
        if row["status"] not in STATUSES:
            errors.append(f"{where}: status must be one of {sorted(STATUSES)}")
    place_order = [(row["source_id"], row["group_key"]) for row in places or []]
    if place_order != sorted(place_order):
        errors.append("overlay_places.csv must be sorted by source_id, then group_key")
    return errors


def _load(
    registry: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    return (
        read_table(registry / "stops.csv", STOP_FIELDS),
        read_table(registry / "posts.csv", POST_FIELDS),
        read_table(registry / "overlay_places.csv", PLACE_FIELDS),
    )


def _fail_on_errors(errors: list[str]) -> None:
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise SystemExit(1)


def run_validate(args) -> None:
    stops, posts, places = _load(args.registry)
    _fail_on_errors(validate(stops, posts, places))
    print(
        json.dumps(
            {
                "overlay_places": len(places),
                "posts": len(posts),
                "stop_ids": len({row["id"] for row in stops}),
                "stop_rows": len(stops),
            },
            sort_keys=True,
        )
    )


def _decision(candidate: dict[str, str], accept_new: bool) -> str:
    decision = (candidate.get("decision") or "").strip()
    if not decision and accept_new and candidate.get("reason") == "new":
        return "new"
    return decision


def _alias_target(candidate: dict[str, str]) -> str:
    target = (candidate.get("alias_of") or "").strip()
    suggestion = (candidate.get("alias_of_suggestion") or "").strip()
    if not target and suggestion and ";" not in suggestion:
        target = suggestion
    return target


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


def promote_stops(
    stops: list[dict[str, str]], candidates: list[dict[str, str]], accept_new: bool
) -> dict[str, int]:
    """``new`` rows sharing a ``provisional_id`` get one new number; ``alias``
    rows add their spelling to ``alias_of`` (or a single suggestion)."""
    known = {(stop_key(row), row["id"]) for row in stops}
    # Re-promoting a file must not register an identity a second time; a
    # same-named stop is new only when it is at least 75 m from every
    # registered one.
    registered: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in stops:
        registered[stop_key(row)].append(row)

    def existing_id(row: dict[str, str]) -> str | None:
        point = _point(row)
        for other in registered.get(stop_key(row), []):
            other_point = _point(other)
            if (
                point is None
                or other_point is None
                or coordinate_distance_metres(*point, *other_point) < DISTINCT_STOP_METRES
            ):
                return other["id"]
        return None

    ids = {row["id"] for row in stops}
    next_id = max((int(row["id"]) for row in stops), default=0) + 1
    allocated: dict[str, str] = {}
    counts = {"added": 0, "aliased": 0, "skipped": 0}
    ordered = sorted(
        candidates,
        key=lambda row: (int(row.get("provisional_id") or 0), stop_key(_as_stop(row, ""))),
    )
    for candidate in ordered:
        decision = _decision(candidate, accept_new)
        if decision == "alias":
            target = _alias_target(candidate)
            if target not in ids:
                raise ValueError(f"alias_of {target!r} is not a registered id")
        elif decision == "new":
            provisional = candidate.get("provisional_id") or ""
            existing = existing_id(_as_stop(candidate, ""))
            if provisional in allocated:
                target = allocated[provisional]
            elif existing is not None:
                target = existing
                if provisional:
                    allocated[provisional] = target
            else:
                target = str(next_id)
                next_id += 1
                ids.add(target)
                counts["added"] += 1
                if provisional:
                    allocated[provisional] = target
        else:
            counts["skipped"] += 1
            continue
        row = _as_stop(candidate, target)
        if (stop_key(row), target) in known:
            counts["skipped"] += 1
            continue
        known.add((stop_key(row), target))
        registered[stop_key(row)].append(row)
        stops.append(row)
        if decision == "alias":
            counts["aliased"] += 1
    stops.sort(key=sort_key)
    return counts


def promote_posts(
    posts: list[dict[str, str]], candidates: list[dict[str, str]], accept_new: bool
) -> dict[str, int]:
    known = {(row["stop_id"], row["post_key"]) for row in posts}
    counts = {"added": 0, "skipped": 0}
    for candidate in candidates:
        key = (candidate["stop_id"].strip(), candidate["post_key"].strip())
        if _decision(candidate, accept_new) != "new" or key in known:
            counts["skipped"] += 1
            continue
        known.add(key)
        posts.append(
            {
                "stop_id": key[0],
                "post_key": key[1],
                "lat": candidate["lat"].strip(),
                "lon": candidate["lon"].strip(),
                "status": "active",
                "note": (candidate.get("note") or "").strip(),
            }
        )
        counts["added"] += 1
    posts.sort(key=lambda row: (int(row["stop_id"]), row["post_key"]))
    return counts


def promote_places(
    places: list[dict[str, str]], candidates: list[dict[str, str]], accept_new: bool
) -> dict[str, int]:
    """``new`` pins the candidate's current place ID; ``alias`` pins the group
    to the place ID in ``alias_of`` (or a single suggestion)."""
    known = {(row["source_id"], row["group_key"]) for row in places}
    pinned = {row["place_id"] for row in places}
    counts = {"added": 0, "aliased": 0, "skipped": 0}
    for candidate in candidates:
        key = (candidate["source_id"].strip(), candidate["group_key"])
        decision = _decision(candidate, accept_new)
        if decision == "new":
            place_id = candidate["place_id"].strip()
        elif decision == "alias":
            place_id = _alias_target(candidate)
            if place_id not in pinned:
                raise ValueError(f"alias_of {place_id!r} is not a pinned place")
        else:
            counts["skipped"] += 1
            continue
        if key in known:
            counts["skipped"] += 1
            continue
        known.add(key)
        pinned.add(place_id)
        places.append(
            {
                "source_id": key[0],
                "group_key": key[1],
                "place_id": place_id,
                "status": "active",
                "note": (candidate.get("note") or candidate.get("stop_name") or "").strip(),
            }
        )
        counts["aliased" if decision == "alias" else "added"] += 1
    places.sort(key=lambda row: (row["source_id"], row["group_key"]))
    return counts


def run_promote(args) -> None:
    """Add reviewed rows from a JrUtil candidates CSV.

    The kind of candidates is taken from the header. A reviewer fills
    ``decision`` with ``new`` or ``alias`` (plus ``alias_of`` when the
    suggestion is not a single value); ``--accept-new`` treats every undecided
    ``new`` row as ``new``, which bootstraps an empty registry.
    """
    stops, posts, places = _load(args.registry)
    _fail_on_errors(validate(stops, posts, places))
    with args.candidates.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or [])
        candidates = list(reader)
    if "provisional_id" in fields:
        counts = promote_stops(stops, candidates, args.accept_new)
    elif "post_key" in fields:
        counts = promote_posts(posts, candidates, args.accept_new)
    elif "group_key" in fields:
        counts = promote_places(places, candidates, args.accept_new)
    else:
        raise SystemExit(f"{args.candidates} is not a JrUtil registry candidates CSV")
    _fail_on_errors(validate(stops, posts, places))
    write_table(args.registry / "stops.csv", STOP_FIELDS, stops)
    write_table(args.registry / "posts.csv", POST_FIELDS, posts)
    write_table(args.registry / "overlay_places.csv", PLACE_FIELDS, places)
    print(json.dumps(counts, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root.add_argument("--registry", type=Path, default=Path(__file__).parent / "registry")
    commands = root.add_subparsers(required=True)
    command = commands.add_parser("validate")
    command.set_defaults(handler=run_validate)
    command = commands.add_parser("promote")
    command.add_argument("candidates", type=Path)
    command.add_argument(
        "--accept-new",
        action="store_true",
        help="treat undecided rows with reason=new as decision=new",
    )
    command.set_defaults(handler=run_promote)
    return root


def main() -> None:
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
