#!/usr/bin/env python3
"""Build the post-international-filter coordinate gap-fill work list.

This is an offline audit. It combines an existing ``gapfill.py audit`` CSV
with the emitted GTFS trip set and timetable kilometres in the merged JDF.
It deliberately does not run JrUtil or contact any geocoding service.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


ADJACENT_COUNTRIES = {"A", "D", "PL", "SK"}
DECLARED_INTERNATIONAL = {"M", "N", "P"}
TRIP_ID = re.compile(r"^jdf:trip:([^:]+):(\d+):(\d+)$")


@dataclass(frozen=True)
class Call:
    trip_id: int
    stop_id: int
    kilometre: Decimal | None


def field(value: str) -> str:
    return value.strip().removesuffix(";")


def country(value: str) -> str:
    normalized = field(value).upper()
    return {"AT": "A", "DE": "D"}.get(normalized, normalized)


def identity_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(character for character in value if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def residual_identity_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        identity_text(row["name"]),
        country(row["country"]) or "CZ",
        identity_text(row["municipality"]),
        row["region"].strip(),
    )


def external_identities(path: Path) -> dict[tuple[str, str, str], list[tuple[float, float]]]:
    """Load conservative exact-name/source-geography identities."""
    result: dict[tuple[str, str, str], list[tuple[float, float]]] = defaultdict(list)
    for source in sorted(path.rglob("*.csv")):
        with source.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.reader(stream):
                if len(row) not in {5, 6}:
                    continue
                name, region, country_code = row[0], row[3].strip(), country(row[4]) or "CZ"
                # OL is the historical JDF code corresponding to the current
                # boundary/source code OC.
                region = {"OL": "OC"}.get(region, region)
                result[(identity_text(name), country_code, region)].append(
                    (float(row[1]), float(row[2]))
                )
    return dict(result)


def covered_by_external(
    row: dict[str, str],
    identities: dict[tuple[str, str, str], list[tuple[float, float]]],
) -> bool:
    expected_name = identity_text(row["name"])
    expected_country = country(row["country"]) or "CZ"
    expected_region = {"OL": "OC"}.get(row["region"].strip(), row["region"].strip())
    candidates = [
        point
        for (name, country_code, region), points in identities.items()
        for point in points
        if name == expected_name
        and country_code == expected_country
        and (not expected_region or not region or region == expected_region)
    ]
    if not candidates:
        return False
    mean_lat = sum(point[0] for point in candidates) / len(candidates)
    mean_lon = sum(point[1] for point in candidates) / len(candidates)

    def distance_metres(point: tuple[float, float]) -> float:
        lat_scale = 111_320.0
        lon_scale = lat_scale * math.cos(math.radians(mean_lat))
        return math.hypot((point[0] - mean_lat) * lat_scale, (point[1] - mean_lon) * lon_scale)

    return max(map(distance_metres, candidates)) < 1_000.0


def emitted_trip_keys(gtfs: Path) -> set[tuple[str, int, int]]:
    result = set()
    with (gtfs / "trips.txt").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            match = TRIP_ID.match(row["trip_id"])
            if not match:
                continue
            result.add((match.group(1), int(match.group(2)), int(match.group(3))))
    return result


def load_jdf(jdf: Path, active_trips: set[tuple[str, int, int]]):
    with zipfile.ZipFile(jdf) as archive:
        def rows(name: str):
            content = archive.read(name).decode("cp1250")
            return csv.reader(content.splitlines())

        routes = {
            (row[0], int(field(row[16]))): {"type": row[3], "name": row[1]}
            for row in rows("Linky.txt")
            if row
        }
        stop_countries = {}
        for row in rows("Zastavky.txt"):
            if not row:
                continue
            value = country(row[5])
            stop_countries[int(row[0])] = value or ("CZ" if row[4].strip() else None)
        integrated = {
            (row[0], int(field(row[6])))
            for row in rows("LinExt.txt")
            if row
        }
        calls: dict[tuple[str, int], list[Call]] = defaultdict(list)
        for row in rows("Zasspoje.txt"):
            if not row:
                continue
            route_id = row[0]
            trip_id = int(row[1])
            distinction = int(field(row[14]))
            if (route_id, distinction, trip_id) not in active_trips:
                continue
            arrival, departure = row[10].strip(), row[11].strip()
            if departure in {"|", "<"} or (not arrival and not departure):
                continue
            calls[(route_id, distinction)].append(
                Call(trip_id, int(row[3]), Decimal(row[9]) if row[9].strip() else None)
            )
    return routes, stop_countries, integrated, calls


def classify_routes(routes, stop_countries, integrated, calls):
    decisions = {}
    for route_key, route in routes.items():
        route_type = route["type"]
        route_calls = calls.get(route_key, [])
        countries_with_unknown = [stop_countries.get(call.stop_id) for call in route_calls]
        countries = sorted({value for value in countries_with_unknown if value})
        foreign = set(countries) - {"CZ"}
        is_international = bool(foreign) or route_type in DECLARED_INTERNATIONAL
        keep, reason = True, "domestic"
        maximum_span = maximum_depth = None
        if is_international:
            if not foreign:
                keep, reason = False, "international_metadata_without_foreign_geography"
            elif any(value is None for value in countries_with_unknown):
                keep, reason = False, "unknown_stop_country"
            elif not foreign.issubset(ADJACENT_COUNTRIES):
                keep, reason = False, "non_adjacent_country"
            else:
                by_trip: dict[int, list[Call]] = defaultdict(list)
                for call in route_calls:
                    by_trip[call.trip_id].append(call)
                international_trips = [
                    trip_calls
                    for trip_calls in by_trip.values()
                    if any(stop_countries[call.stop_id] != "CZ" for call in trip_calls)
                ]
                if any(
                    not any(stop_countries[call.stop_id] == "CZ" for call in trip_calls)
                    for trip_calls in international_trips
                ):
                    keep, reason = False, "foreign_only_trip"
                elif not international_trips or any(
                    call.kilometre is None
                    for trip_calls in international_trips
                    for call in trip_calls
                ):
                    keep, reason = False, "missing_timetable_kilometres"
                else:
                    metrics = []
                    for trip_calls in international_trips:
                        czech_km = [
                            call.kilometre
                            for call in trip_calls
                            if stop_countries[call.stop_id] == "CZ"
                        ]
                        foreign_km = [
                            call.kilometre
                            for call in trip_calls
                            if stop_countries[call.stop_id] != "CZ"
                        ]
                        all_km = [call.kilometre for call in trip_calls]
                        span = max(all_km) - min(all_km)
                        depth = max(min(abs(value - cz) for cz in czech_km) for value in foreign_km)
                        metrics.append((span, depth))
                    maximum_span = max(value[0] for value in metrics)
                    maximum_depth = max(value[1] for value in metrics)
                    span_limit, depth_limit = ((Decimal(200), Decimal(80))
                                               if route_key in integrated
                                               else (Decimal(120), Decimal(60)))
                    if maximum_span > span_limit:
                        keep, reason = False, "trip_span_exceeds_limit"
                    elif maximum_depth > depth_limit:
                        keep, reason = False, "foreign_depth_exceeds_limit"
                    else:
                        keep, reason = True, "regional_adjacent"
        decisions[route_key] = {
            "keep": keep,
            "reason": reason,
            "countries": countries,
            "integrated": route_key in integrated,
            "maximum_span": str(maximum_span) if maximum_span is not None else "",
            "maximum_depth": str(maximum_depth) if maximum_depth is not None else "",
        }
    return decisions


def retained_missing_stops(gtfs: Path, unresolved_ids: set[str], decisions):
    routes_by_stop: dict[str, set[tuple[str, int]]] = defaultdict(set)
    with (gtfs / "stop_times.txt").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            stop_id = row["stop_id"]
            if stop_id not in unresolved_ids:
                continue
            match = TRIP_ID.match(row["trip_id"])
            if not match:
                continue
            route_key = match.group(1), int(match.group(2))
            if decisions.get(route_key, {}).get("keep"):
                routes_by_stop[stop_id].add(route_key)
    return routes_by_stop


def stage_for(name: str, country_code: str) -> str:
    normalized = identity_text(name)
    border_tokens = {"clo", "zoll", "grenze", "statni hranice", "st hr"}
    if any(token in normalized for token in border_tokens):
        return "manual-border-call"
    if country_code == "CZ":
        return "targeted-cz-osm"
    return "targeted-europe-osm"


def apply_context_overrides(
    unresolved: dict[str, dict[str, str]], overrides_path: Path | None
) -> None:
    if overrides_path is None:
        return
    with overrides_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream)
        required = {"stop_id", "municipality", "reason"}
        if not rows.fieldnames or not required.issubset(rows.fieldnames):
            raise ValueError(f"{overrides_path} must contain {', '.join(sorted(required))}")
        seen = set()
        for row in rows:
            stop_id = row["stop_id"].strip()
            municipality = row["municipality"].strip()
            if stop_id in seen:
                raise ValueError(f"Duplicate context override for {stop_id}")
            if stop_id not in unresolved:
                raise ValueError(f"Context override refers to unknown stop {stop_id}")
            if not municipality or not row["reason"].strip():
                raise ValueError(f"Context override for {stop_id} requires municipality and reason")
            seen.add(stop_id)
            unresolved[stop_id]["municipality"] = municipality


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("gtfs", type=Path)
    parser.add_argument("jdf", type=Path)
    parser.add_argument("unresolved_audit", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--external-geodata", type=Path)
    parser.add_argument("--context-overrides", type=Path)
    args = parser.parse_args()

    with args.unresolved_audit.open(encoding="utf-8-sig", newline="") as stream:
        unresolved = {row["stop_id"]: row for row in csv.DictReader(stream)}
    apply_context_overrides(unresolved, args.context_overrides)
    active_trips = emitted_trip_keys(args.gtfs)
    routes, stop_countries, integrated, calls = load_jdf(args.jdf, active_trips)
    decisions = classify_routes(routes, stop_countries, integrated, calls)
    routes_by_stop = retained_missing_stops(args.gtfs, set(unresolved), decisions)
    source_identities = (
        external_identities(args.external_geodata) if args.external_geodata else {}
    )
    covered_stop_ids = {
        stop_id for stop_id in routes_by_stop
        if covered_by_external(unresolved[stop_id], source_identities)
    }
    routes_by_stop = {
        stop_id: route_keys for stop_id, route_keys in routes_by_stop.items()
        if stop_id not in covered_stop_ids
    }

    identities: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for stop_id, route_keys in routes_by_stop.items():
        row = unresolved[stop_id]
        identities[residual_identity_key(row)].append(row)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "identity_id", "name", "municipality", "region", "country", "stop_ids",
        "route_distinctions", "route_names", "stage", "fallback_order",
    ]
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for key, rows in sorted(
            identities.items(), key=lambda item: (item[0][1], item[0][0], item[0][2], item[0][3])
        ):
            stop_ids = sorted(row["stop_id"] for row in rows)
            route_keys = sorted({route for stop_id in stop_ids for route in routes_by_stop[stop_id]})
            representative = rows[0]
            digest = hashlib.sha1(":".join(key).encode()).hexdigest()[:12]
            writer.writerow({
                "identity_id": digest,
                "name": representative["name"],
                "municipality": representative["municipality"],
                "region": representative["region"],
                "country": key[1],
                "stop_ids": ";".join(stop_ids),
                "route_distinctions": ";".join(f"{route}/{distinction}" for route, distinction in route_keys),
                "route_names": ";".join(sorted({routes[route]["name"] for route in route_keys})),
                "stage": stage_for(representative["name"], key[1]),
                "fallback_order": "open-catalogue>OSM>Mapy>town>unresolved",
            })

    cross_border = [value for value in decisions.values() if any(c != "CZ" for c in value["countries"])]
    summary = {
        "input_missing_stop_ids": len(unresolved),
        "covered_by_refreshed_external_data": len(covered_stop_ids),
        "retained_missing_stop_ids": len(routes_by_stop),
        "retained_missing_identities": len(identities),
        "retained_cross_border_routes": sum(value["keep"] for value in cross_border),
        "rejected_cross_border_routes": sum(not value["keep"] for value in cross_border),
        "by_country": dict(sorted(
            (code, sum(1 for key in identities if key[1] == code))
            for code in {key[1] for key in identities}
        )),
        "by_stage": dict(sorted(
            (stage, sum(1 for rows in identities.values() if stage_for(rows[0]["name"], country(rows[0]["country"])) == stage))
            for stage in {stage_for(rows[0]["name"], country(rows[0]["country"])) for rows in identities.values()}
        )),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
