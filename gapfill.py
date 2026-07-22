#!/usr/bin/env python3
"""One-shot OSM and Mapy coordinate gap filling.

The input is a UTF-8 CSV with ``stop_id,name,municipality,region,country``.
Mapy may also refine route-derived estimated coordinates. Its name matching is
deliberately permissive, but candidates must still pass the same scheduled-time
distance ceiling used by JrUtil. Raw service responses are cached only for OSM
maintenance and are never retained for Mapy.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import requests

from download import iso_country_code, normalize_country_code, region_for_coordinates

USER_AGENT = "jrunify-ext-geodata/1.0 (+https://gitlab.com/dvdkon/jrunify-ext-geodata)"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MAPY_URL = "https://api.mapy.com/v1/geocode"


@dataclass(frozen=True)
class StopQuery:
    stop_id: str
    name: str
    municipality: str
    region: str
    country: str
    coordinate_status: str = "missing"
    latitude: float | None = None
    longitude: float | None = None
    coordinate_source: str = ""
    route_occurrences: int = 0
    route_contexts: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Candidate:
    names: tuple[str, ...]
    latitude: float
    longitude: float
    municipality: str
    country: str
    kind: str = "stop"
    region: str = ""
    source: str = ""
    rank: int = 0
    label: str = ""
    query: str = ""


ABBREVIATIONS = {
    "abzw": "abzweigung",
    "aut nadr": "autobusove nadrazi",
    "aut st": "autobusove stanoviste",
    "cint": "cintorin",
    "dw aut": "dworzec autobusowy",
    "hl nadr": "hlavni nadrazi",
    "odb": "odbocka",
    "kriz": "krizovatka",
    "nam": "namesti",
    "nem": "nemocnice",
    "razc": "razcestie",
    "zel st": "zeleznicni stanice",
}

QUERY_ABBREVIATIONS = (
    (re.compile(r"\bodb\.?", re.IGNORECASE), "odbočka"),
    (re.compile(r"\bkřiž(?:\.|\b)", re.IGNORECASE), "křižovatka"),
    (re.compile(r"\bkriz(?:\.|\b)", re.IGNORECASE), "křižovatka"),
    (re.compile(r"\baut\.?\s*n(?:á|a)dr\.?", re.IGNORECASE), "autobusové nádraží"),
    (re.compile(r"\bžel\.?\s*st\.?", re.IGNORECASE), "železniční stanice"),
)

MUNICIPALITY_ALIASES = {
    "bayer eisenstein": "bayerisch eisenstein",
    "furth i w": "furth im wald",
    "petrovice u karv": "petrovice u karvine",
    "blatnice p sv antoninkem": "blatnice pod svatym antoninkem",
    "schrattenberg no": "schrattenberg",
    "falkenstein": "falkenstein im weinviertel",
    "visla": "wisla",
    "javorinka": "jaworzynka",
}


def normalize_text(value: str) -> str:
    value = value.casefold().translate(
        str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss"})
    )
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " a ")
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    for short, expanded in ABBREVIATIONS.items():
        value = re.sub(rf"\b{re.escape(short)}\b", expanded, value)
    return " ".join(value.split())


def stop_name_identities(stop: StopQuery) -> set[str]:
    components = [part.strip() for part in stop.name.split(",") if part.strip()]
    identities = {normalize_text(stop.name)}
    if components:
        identities.add(normalize_text(components[-1]))
    if (
        stop.municipality
        and components
        and normalize_text(components[0]) == normalize_text(stop.municipality)
    ):
        identities.add(normalize_text(" ".join(components[1:])))
    if stop.municipality and (
        not components
        or normalize_text(components[0]) != normalize_text(stop.municipality)
    ):
        identities.add(normalize_text(f"{stop.municipality},{stop.name}"))
    return identities - {""}


def component_identities(value: str) -> set[str]:
    """Return full and locality-qualified suffix identities for a stop name."""
    components = [part.strip() for part in value.split(",") if part.strip()]
    identities = {normalize_text(value)}
    identities.update(
        normalize_text(" ".join(components[index:])) for index in range(len(components))
    )
    return identities - {""}


def exact_name_matches(stop: StopQuery, candidate: Candidate) -> bool:
    expected = {
        variant
        for name in stop_name_identities(stop)
        for variant in identity_variants(name)
    }
    actual = {
        variant
        for name in candidate.names
        for identity in component_identities(name)
        for variant in identity_variants(identity)
    }
    return bool(expected.intersection(actual))


def expanded_query(value: str) -> str:
    for pattern, replacement in QUERY_ABBREVIATIONS:
        value = pattern.sub(replacement, value)
    return " ".join(value.split())


def mapy_query_variants(stop: StopQuery) -> tuple[str, ...]:
    components = [part.strip() for part in stop.name.split(",") if part.strip()]
    variants = [stop.name, expanded_query(stop.name)]
    if components:
        for index in range(1, len(components)):
            suffix = ", ".join(components[index:])
            variants.extend((suffix, f"{suffix}, {stop.municipality or components[0]}"))
        variants.append(f"{components[-1]}, {stop.municipality or components[0]}")
    return tuple(dict.fromkeys(value.strip() for value in variants if value.strip()))


def identity_variants(value: str) -> set[str]:
    normalized = normalize_text(value)
    return {normalized, " ".join(sorted(normalized.split()))} - {""}


def normalized_municipality(value: str) -> str:
    normalized = normalize_text(value)
    return MUNICIPALITY_ALIASES.get(normalized, normalized)


def municipality_matches(stop: StopQuery, candidate: Candidate) -> bool:
    expected = normalized_municipality(stop.municipality or stop.name.split(",", 1)[0])
    actual = normalized_municipality(candidate.municipality)
    if not expected:
        return True
    if not actual:
        return bool(stop.region and candidate.region == stop.region)
    if actual == expected:
        return True
    stop_components = {
        normalized_municipality(part) for part in stop.name.split(",") if part.strip()
    }
    candidate_components = {
        normalized_municipality(part)
        for name in candidate.names
        for part in name.split(",")
        if part.strip()
    }
    return actual in stop_components or expected in candidate_components


def _country_matches(expected: str, actual: str) -> bool:
    expected = normalize_country_code(expected) or "CZ"
    actual = normalize_country_code(actual) or "CZ"
    return expected == actual


def distance_metres(left: Candidate, right: Candidate) -> float:
    latitude = math.radians((left.latitude + right.latitude) / 2)
    north = (left.latitude - right.latitude) * 111_320
    east = (left.longitude - right.longitude) * 111_320 * math.cos(latitude)
    return math.hypot(north, east)


def choose_exact_candidate(
    stop: StopQuery, candidates: list[Candidate]
) -> Candidate | None:
    municipality = normalized_municipality(
        stop.municipality or stop.name.split(",", 1)[0]
    )
    accepted = []
    for candidate in candidates:
        if candidate.kind != "stop":
            continue
        if not _country_matches(stop.country, candidate.country):
            continue
        if (
            municipality
            and not municipality_matches(stop, candidate)
            and not (
                stop.coordinate_status == "estimated"
                and exact_name_matches(stop, candidate)
            )
        ):
            continue
        if not exact_name_matches(stop, candidate):
            continue
        accepted.append(candidate)
    unique_positions = {(item.latitude, item.longitude): item for item in accepted}
    positions = list(unique_positions.values())
    if len(positions) == 1:
        return positions[0]
    if positions and all(
        distance_metres(left, right) <= 500
        for index, left in enumerate(positions)
        for right in positions[index + 1 :]
    ):
        return platform_centroid(positions)
    clusters: list[list[Candidate]] = []
    for position in positions:
        matching = [
            cluster
            for cluster in clusters
            if any(distance_metres(position, item) <= 500 for item in cluster)
        ]
        if not matching:
            clusters.append([position])
        else:
            matching[0].append(position)
    largest = sorted(clusters, key=len, reverse=True)
    if (
        largest
        and len(largest[0]) >= 2
        and (len(largest) == 1 or len(largest[0]) > len(largest[1]))
        and any(item.rank == 0 for item in largest[0])
    ):
        return platform_centroid(largest[0])
    return None


def platform_centroid(positions: list[Candidate]) -> Candidate:
    representative = positions[0]
    return Candidate(
        representative.names,
        sum(item.latitude for item in positions) / len(positions),
        sum(item.longitude for item in positions) / len(positions),
        representative.municipality,
        representative.country,
        "stop",
        representative.region,
        f"{representative.source}-platform-centroid",
    )


def choose_fuzzy_candidate(
    stop: StopQuery, candidates: list[Candidate], threshold: float
) -> Candidate | None:
    municipality = normalized_municipality(
        stop.municipality or stop.name.split(",", 1)[0]
    )
    targets = stop_name_identities(stop)
    scored_positions: dict[tuple[float, float], tuple[float, Candidate]] = {}
    for candidate in candidates:
        if candidate.kind != "stop" or not _country_matches(
            stop.country, candidate.country
        ):
            continue
        if municipality and not municipality_matches(stop, candidate):
            continue
        score = max(
            difflib.SequenceMatcher(
                None, normalize_text(target), normalize_text(name)
            ).ratio()
            for target in targets
            for name in candidate.names
        )
        position = candidate.latitude, candidate.longitude
        if position not in scored_positions or score > scored_positions[position][0]:
            scored_positions[position] = score, candidate
    ranked = sorted(scored_positions.values(), key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < threshold:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.1:
        return None
    return ranked[0][1]


def candidate_name_score(stop: StopQuery, candidate: Candidate) -> float:
    targets = stop_name_identities(stop)
    return max(
        difflib.SequenceMatcher(
            None, normalize_text(target), normalize_text(name)
        ).ratio()
        for target in targets
        for name in candidate.names
    )


def coordinate_distance_metres(
    latitude: float, longitude: float, other_latitude: float, other_longitude: float
) -> float:
    middle_latitude = math.radians((latitude + other_latitude) / 2)
    north = (latitude - other_latitude) * 111_320
    east = (longitude - other_longitude) * 111_320 * math.cos(middle_latitude)
    return math.hypot(north, east)


def route_feasibility(
    stop: StopQuery, candidate: Candidate
) -> tuple[bool, str, float | None]:
    distance_from_estimate = (
        coordinate_distance_metres(
            stop.latitude, stop.longitude, candidate.latitude, candidate.longitude
        )
        if stop.latitude is not None and stop.longitude is not None
        else None
    )
    if stop.coordinate_status != "estimated":
        return True, "not_estimated", distance_from_estimate

    anchors = []
    for context in stop.route_contexts:
        anchors.extend(
            anchor
            for anchor in (context.get("previous"), context.get("following"))
            if anchor
        )
    for anchor in anchors:
        elapsed = float(anchor["elapsed_minutes"])
        distance = coordinate_distance_metres(
            candidate.latitude,
            candidate.longitude,
            float(anchor["latitude"]),
            float(anchor["longitude"]),
        )
        # This is intentionally identical to JdfFixups.rejectImplausibleMatches:
        # 2 km local slack plus a deliberately generous 150 km/h.
        maximum = 2_000 + elapsed * 2_500
        if elapsed < 0 or distance > maximum:
            return False, "route_time_impossible", distance_from_estimate
    if anchors:
        return True, "route_time_feasible", distance_from_estimate
    if (
        distance_from_estimate is not None
        and distance_from_estimate <= 2_000
        and municipality_matches(stop, candidate)
    ):
        return True, "estimate_within_2km", distance_from_estimate
    return False, "no_route_context_or_nearby_match", distance_from_estimate


def candidate_geography(
    stop: StopQuery, candidate: Candidate
) -> tuple[bool, str, float | None]:
    if candidate.kind != "stop":
        return False, "not_public_transport_stop", None
    if not _country_matches(stop.country, candidate.country):
        return False, "wrong_country", None
    route_accepted, route_reason, distance = route_feasibility(stop, candidate)
    if not route_accepted:
        return False, route_reason, distance
    if not municipality_matches(stop, candidate):
        if stop.coordinate_status == "estimated" and exact_name_matches(
            stop, candidate
        ):
            return True, "route_exact_locality_alias", distance
        return False, "municipality_mismatch", distance
    return True, route_reason, distance


def load_queries(path: Path) -> list[StopQuery]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream)
        required = {"name", "municipality", "region", "country"}
        if (
            not rows.fieldnames
            or not required.issubset(rows.fieldnames)
            or not ({"stop_id", "stop_ids"} & set(rows.fieldnames))
        ):
            raise ValueError(
                f"{path} must contain {', '.join(sorted(required))} and stop_id or stop_ids"
            )
        queries = []
        for row in rows:
            contexts = json.loads(row.get("route_contexts") or "[]")
            queries.append(
                StopQuery(
                    (row.get("stop_id") or row.get("stop_ids") or "").strip(),
                    row["name"].strip(),
                    row["municipality"].strip(),
                    row["region"].strip(),
                    row["country"].strip(),
                    (row.get("coordinate_status") or "missing").strip(),
                    _optional_float(row.get("current_latitude")),
                    _optional_float(row.get("current_longitude")),
                    (row.get("coordinate_source") or "").strip(),
                    int(row.get("route_occurrences") or 0),
                    tuple(contexts),
                )
            )
        return queries


def _optional_float(value: str | None) -> float | None:
    return float(value) if value and value.strip() else None


def load_stop_metadata(path: Path) -> dict[str, dict]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as error:
        raise RuntimeError(
            "Reading estimated coordinates requires PyArrow; run with "
            "`uv run --with pyarrow --with requests python gapfill.py ...`"
        ) from error
    return {row["gtfs_stop_id"]: row for row in parquet.read_table(path).to_pylist()}


def _clock_minutes(value: str) -> float | None:
    if not value:
        return None
    hours, minutes, seconds = value.split(":")
    return int(hours) * 60 + int(minutes) + int(seconds) / 60


def _stop_place_id(row: dict[str, str]) -> str:
    return row.get("parent_station") or row["stop_id"]


def _route_anchor(
    calls: list[tuple[str, float | None]],
    index: int,
    direction: int,
    target_time: float,
    coordinates: dict[str, tuple[float, float]],
    precisions: dict[str, str],
) -> dict | None:
    cursor = index + direction
    while 0 <= cursor < len(calls):
        stop_id, anchor_time = calls[cursor]
        point = coordinates.get(stop_id)
        if (
            anchor_time is not None
            and point is not None
            and precisions.get(stop_id) == "stop"
        ):
            elapsed = (
                (target_time - anchor_time)
                if direction < 0
                else (anchor_time - target_time)
            )
            if elapsed >= 0:
                return {
                    "stop_id": stop_id,
                    "elapsed_minutes": round(elapsed, 6),
                    "latitude": point[0],
                    "longitude": point[1],
                }
        cursor += direction
    return None


def scan_route_contexts(
    stop_times_path: Path,
    boarding_to_place: dict[str, str],
    targets: set[str],
    coordinates: dict[str, tuple[float, float]],
    precisions: dict[str, str],
) -> tuple[set[str], dict[str, int], dict[str, list[dict]]]:
    referenced: set[str] = set()
    occurrences: dict[str, int] = defaultdict(int)
    contexts: dict[str, dict[str, dict]] = defaultdict(dict)

    def process(trip_id: str, calls: list[tuple[str, float | None]]) -> None:
        # A one-distinct-stop trip is invalid and is ignored even when auditing
        # an older bundle produced before JrUtil learned to drop it.
        if len({stop_id for stop_id, _ in calls}) < 2:
            return
        referenced.update(stop_id for stop_id, _ in calls)
        for index, (stop_id, call_time) in enumerate(calls):
            if stop_id not in targets:
                continue
            occurrences[stop_id] += 1
            if call_time is None:
                continue
            previous = _route_anchor(
                calls, index, -1, call_time, coordinates, precisions
            )
            following = _route_anchor(
                calls, index, 1, call_time, coordinates, precisions
            )
            if previous is None and following is None:
                continue
            context = {
                "sample_trip_id": trip_id,
                "previous": previous,
                "following": following,
            }
            key = json.dumps(
                {"previous": previous, "following": following}, sort_keys=True
            )
            existing = contexts[stop_id].get(key)
            if existing is None:
                context["occurrences"] = 1
                contexts[stop_id][key] = context
            else:
                existing["occurrences"] += 1

    with stop_times_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream)
        current_trip = ""
        calls: list[tuple[str, float | None]] = []
        for row in rows:
            trip_id = row["trip_id"]
            if current_trip and trip_id != current_trip:
                process(current_trip, calls)
                calls = []
            current_trip = trip_id
            calls.append(
                (
                    boarding_to_place.get(row["stop_id"], row["stop_id"]),
                    _clock_minutes(
                        row.get("arrival_time") or row.get("departure_time") or ""
                    ),
                )
            )
        if current_trip:
            process(current_trip, calls)
    return (
        referenced,
        occurrences,
        {stop_id: list(values.values()) for stop_id, values in contexts.items()},
    )


def run_audit(args) -> None:
    with (args.gtfs / "stops.txt").open(encoding="utf-8-sig", newline="") as stream:
        stops = list(csv.DictReader(stream))
    stop_places = {
        row["stop_id"]: row
        for row in stops
        if row.get("location_type", "") == "1" or not row.get("parent_station")
    }
    boarding_to_place = {row["stop_id"]: _stop_place_id(row) for row in stops}
    coordinates = {
        stop_id: (float(row["stop_lat"]), float(row["stop_lon"]))
        for stop_id, row in stop_places.items()
        if float(row["stop_lat"]) != 0 or float(row["stop_lon"]) != 0
    }
    metadata_path = args.metadata or args.gtfs.parent / "source_stop_metadata.parquet"
    metadata = load_stop_metadata(metadata_path)
    precisions = {
        stop_id: row["coordinate_precision"] for stop_id, row in metadata.items()
    }
    wanted = (
        {"missing", "estimated"}
        if args.coordinate_status == "all"
        else {args.coordinate_status}
    )
    targets = {
        stop_id for stop_id, precision in precisions.items() if precision in wanted
    }
    referenced, occurrences, contexts = scan_route_contexts(
        args.gtfs / "stop_times.txt",
        boarding_to_place,
        targets,
        coordinates,
        precisions,
    )
    selected = sorted(targets.intersection(referenced))
    with zipfile.ZipFile(args.jdf) as archive:
        rows = csv.reader(archive.read("Zastavky.txt").decode("cp1250").splitlines())
        source_stops = {}
        for row in rows:
            if not row:
                continue
            source_stops[row[0]] = StopQuery(
                "",
                ",".join(value.strip() for value in row[1:4] if value.strip()),
                row[1].strip(),
                row[4].strip(),
                normalize_country_code(row[5]) or "CZ",
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "stop_id",
                "name",
                "municipality",
                "region",
                "country",
                "coordinate_status",
                "current_latitude",
                "current_longitude",
                "coordinate_source",
                "route_occurrences",
                "route_contexts",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        for stop_id in selected:
            match = re.fullmatch(r"jdf:stop:(\d+)", stop_id)
            if not match or match.group(1) not in source_stops:
                raise ValueError(f"Cannot join GTFS stop to merged JDF: {stop_id}")
            source = source_stops[match.group(1)]
            point = coordinates.get(stop_id)
            meta = metadata[stop_id]
            writer.writerow(
                {
                    "stop_id": stop_id,
                    "name": source.name,
                    "municipality": source.municipality,
                    "region": source.region,
                    "country": source.country,
                    "coordinate_status": meta["coordinate_precision"],
                    "current_latitude": point[0] if point else "",
                    "current_longitude": point[1] if point else "",
                    "coordinate_source": meta.get("coordinate_source") or "",
                    "route_occurrences": occurrences.get(stop_id, 0),
                    "route_contexts": json.dumps(
                        contexts.get(stop_id, []),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )
    print(
        json.dumps(
            {
                "emitted_stops": len(stops),
                "coordinate_status": args.coordinate_status,
                "referenced_selected_coordinates": len(selected),
                "referenced_stop_places": len(referenced),
                "referenced_with_other_coordinates": len(referenced) - len(selected),
            },
            sort_keys=True,
        )
    )


def write_results(
    accepted_path: Path,
    review_path: Path,
    accepted: list[tuple[StopQuery, Candidate]],
    review: list[tuple[StopQuery, str, int]],
) -> None:
    accepted_path.parent.mkdir(parents=True, exist_ok=True)
    with accepted_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        for stop, candidate in sorted(accepted, key=lambda pair: pair[0].stop_id):
            writer.writerow(
                [
                    stop.name,
                    candidate.latitude,
                    candidate.longitude,
                    stop.region,
                    normalize_country_code(stop.country) or "CZ",
                ]
            )
    with review_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            [
                "stop_id",
                "name",
                "municipality",
                "region",
                "country",
                "coordinate_status",
                "current_latitude",
                "current_longitude",
                "coordinate_source",
                "route_occurrences",
                "route_contexts",
                "query_variants",
                "reason",
                "candidates",
            ]
        )
        for stop, reason, count in sorted(review, key=lambda item: item[0].stop_id):
            writer.writerow(
                [
                    stop.stop_id,
                    stop.name,
                    stop.municipality,
                    stop.region,
                    stop.country,
                    stop.coordinate_status,
                    stop.latitude if stop.latitude is not None else "",
                    stop.longitude if stop.longitude is not None else "",
                    stop.coordinate_source,
                    stop.route_occurrences,
                    json.dumps(
                        stop.route_contexts, ensure_ascii=False, separators=(",", ":")
                    ),
                    " | ".join(mapy_query_variants(stop)),
                    reason,
                    count,
                ]
            )


class JsonHttpClient:
    def __init__(self, session=None, retries: int = 3, delay: float = 1.0):
        self.session = session or requests.Session()
        self.retries = retries
        self.delay = delay

    def request(self, method: str, url: str, **kwargs):
        last_error = None
        for attempt in range(self.retries):
            try:
                response = self.session.request(method, url, timeout=60, **kwargs)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as error:
                last_error = error
                if attempt + 1 < self.retries:
                    time.sleep(self.delay * 2**attempt)
        raise RuntimeError(
            f"Request failed after {self.retries} attempts"
        ) from last_error


class OverpassClient(JsonHttpClient):
    def __init__(self, endpoint: str = OVERPASS_URL, **kwargs):
        super().__init__(**kwargs)
        self.endpoint = endpoint

    def extract(
        self, bbox: str, cache: Path, cached_only: bool = False
    ) -> list[dict] | None:
        cache.mkdir(parents=True, exist_ok=True)
        cache_file = cache / (re.sub(r"[^0-9.-]+", "_", bbox) + ".json")
        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))["elements"]
        if cached_only:
            return None
        selectors = (
            '[public_transport~"platform|stop_position|station"]',
            '[highway="bus_stop"]',
            '[railway~"station|halt|tram_stop|platform"]',
            '[amenity="bus_station"]',
            '[type="public_transport"][public_transport="stop_area"]',
        )
        query = (
            "[out:json][timeout:180];("
            + "".join(f"nwr{selector}({bbox});" for selector in selectors)
            + ");out tags center;"
        )
        payload = self.request(
            "POST",
            self.endpoint,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
        )
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload["elements"]


def osm_candidates(
    elements: list[dict], fallback_municipality: str = "", fallback_country: str = "CZ"
) -> list[Candidate]:
    result = []
    for element in elements:
        tags = element.get("tags", {})
        names = []
        for key in ("name", "name:cs", "official_name", "short_name"):
            if tags.get(key):
                names.append(tags[key])
        if tags.get("alt_name"):
            names.extend(
                part.strip() for part in tags["alt_name"].split(";") if part.strip()
            )
        position = element.get("center", element)
        if not names or "lat" not in position or "lon" not in position:
            continue
        municipality = next(
            (
                tags.get(key, "")
                for key in ("addr:city", "is_in:city", "is_in")
                if tags.get(key)
            ),
            fallback_municipality,
        )
        country = tags.get(
            "addr:country", tags.get("is_in:country_code", fallback_country)
        )
        result.append(
            Candidate(
                tuple(names),
                float(position["lat"]),
                float(position["lon"]),
                municipality,
                country,
                region=region_for_coordinates(
                    float(position["lat"]), float(position["lon"])
                )
                or "",
            )
        )
    return result


class NominatimClient(JsonHttpClient):
    def municipality(
        self, name: str, country: str, cache: Path
    ) -> tuple[float, float] | None:
        cache.mkdir(parents=True, exist_ok=True)
        key = normalize_text(f"{country}-{name}").replace(" ", "-")
        cached = cache / f"{key}.json"
        if cached.exists():
            payload = json.loads(cached.read_text(encoding="utf-8"))
        else:
            time.sleep(1.0)
            payload = self.request(
                "GET",
                NOMINATIM_URL,
                params={
                    "q": name,
                    "countrycodes": (iso_country_code(country) or country).lower(),
                    "format": "jsonv2",
                    "limit": 2,
                },
                headers={"User-Agent": USER_AGENT},
            )
            cached.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return (
            (float(payload[0]["lat"]), float(payload[0]["lon"]))
            if len(payload) == 1
            else None
        )

    def search(self, stop: StopQuery, cache: Path) -> list[Candidate]:
        cache.mkdir(parents=True, exist_ok=True)
        key = normalize_text(
            f"stop-{stop.country}-{stop.name}-{stop.municipality}"
        ).replace(" ", "-")
        cached = cache / f"{key}.json"
        if cached.exists():
            payload = json.loads(cached.read_text(encoding="utf-8"))
        else:
            time.sleep(max(1.0, self.delay))
            payload = self.request(
                "GET",
                NOMINATIM_URL,
                params={
                    "q": f"{stop.name}, {stop.municipality}",
                    "countrycodes": (
                        iso_country_code(stop.country) or stop.country
                    ).lower(),
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "namedetails": 1,
                    "limit": 5,
                },
                headers={"User-Agent": USER_AGENT},
            )
            cached.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        candidates = []
        for item in payload:
            address = item.get("address", {})
            names = [item.get("name", "")]
            names.extend(item.get("namedetails", {}).values())
            names = tuple(dict.fromkeys(name for name in names if name))
            if not names:
                continue
            municipality = next(
                (
                    address.get(field, "")
                    for field in ("city", "town", "village", "municipality", "hamlet")
                    if address.get(field)
                ),
                stop.municipality
                if normalize_text(stop.municipality)
                in normalize_text(item.get("display_name", ""))
                else "",
            )
            category = item.get("category", "")
            result_type = item.get("type", "")
            is_stop = category in {
                "highway",
                "public_transport",
                "railway",
            } or result_type in {
                "bus_stop",
                "platform",
                "station",
                "halt",
                "tram_stop",
            }
            candidates.append(
                Candidate(
                    names,
                    float(item["lat"]),
                    float(item["lon"]),
                    municipality,
                    address.get("country_code", stop.country),
                    "stop" if is_stop else "poi",
                    region=region_for_coordinates(
                        float(item["lat"]), float(item["lon"])
                    )
                    or "",
                    source="nominatim-search",
                )
            )
        return candidates


def municipality_bbox(position: tuple[float, float], radius_degrees: float) -> str:
    latitude, longitude = position
    longitude_radius = radius_degrees / max(0.35, abs(math.cos(math.radians(latitude))))
    return ",".join(
        f"{value:.6f}"
        for value in (
            latitude - radius_degrees,
            longitude - longitude_radius,
            latitude + radius_degrees,
            longitude + longitude_radius,
        )
    )


def mapy_bbox(position: tuple[float, float], radius_degrees: float = 0.2) -> str:
    latitude, longitude = position
    longitude_radius = radius_degrees / max(0.35, abs(math.cos(math.radians(latitude))))
    return ",".join(
        f"{value:.6f}"
        for value in (
            longitude - longitude_radius,
            latitude - radius_degrees,
            longitude + longitude_radius,
            latitude + radius_degrees,
        )
    )


def gtfs_municipality_centers(
    gtfs: Path, jdf: Path
) -> dict[tuple[str, str, str], tuple[float, float]]:
    with zipfile.ZipFile(jdf) as archive:
        source_stops = {}
        rows = csv.reader(archive.read("Zastavky.txt").decode("cp1250").splitlines())
        for row in rows:
            if not row:
                continue
            source_stops[row[0]] = (
                normalize_country_code(row[5]) or "CZ",
                normalize_text(row[1]),
                row[4].strip(),
            )
    points: dict[tuple[str, str, str], list[tuple[float, float]]] = defaultdict(list)
    with (gtfs / "stops.txt").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            match = re.match(r"jdf:stop:(\d+):", row["stop_id"])
            if not match or match.group(1) not in source_stops:
                continue
            latitude, longitude = float(row["stop_lat"]), float(row["stop_lon"])
            if latitude == 0 and longitude == 0:
                continue
            points[source_stops[match.group(1)]].append((latitude, longitude))
    return {
        key: (
            sum(point[0] for point in values) / len(values),
            sum(point[1] for point in values) / len(values),
        )
        for key, values in points.items()
    }


class MapyClient(JsonHttpClient):
    def __init__(self, api_key: str, municipality_centers=None, **kwargs):
        super().__init__(**kwargs)
        if not api_key:
            raise ValueError("MAPY_API_KEY is required")
        self.api_key = api_key
        self.municipality_centers = municipality_centers or {}

    def municipality_center(self, stop: StopQuery) -> tuple[float, float] | None:
        if stop.latitude is not None and stop.longitude is not None:
            return stop.latitude, stop.longitude
        country = normalize_country_code(stop.country) or "CZ"
        name = normalize_text(stop.municipality)
        exact = self.municipality_centers.get((country, name, stop.region))
        if exact is not None:
            return exact
        matches = [
            center
            for (
                center_country,
                center_name,
                _,
            ), center in self.municipality_centers.items()
            if center_country == country and center_name == name
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def preference_radius(stop: StopQuery) -> int:
        budgets = [
            2_000 + float(anchor["elapsed_minutes"]) * 2_500
            for context in stop.route_contexts
            for anchor in (context.get("previous"), context.get("following"))
            if anchor
        ]
        return round(min(20_000, max([2_000, *budgets])))

    def geocode(self, stop: StopQuery) -> list[Candidate]:
        country_locality = iso_country_code(stop.country) or "CZ"
        center = self.municipality_center(stop)
        candidates = []
        for query in mapy_query_variants(stop):
            params = {
                "query": query,
                "lang": "cs",
                "limit": 5,
                "type": "poi",
                "locality": country_locality,
            }
            if center is not None:
                params.update(
                    {
                        "preferNear": f"{center[1]:.7f},{center[0]:.7f}",
                        "preferNearPrecision": self.preference_radius(stop),
                    }
                )
            payload = self.request(
                "GET",
                MAPY_URL,
                params=params,
                headers={"X-MAPY-API-KEY": self.api_key, "User-Agent": USER_AGENT},
            )
            for rank, entity in enumerate(
                payload.get("items", payload.get("entities", []))[:5]
            ):
                regional = entity.get("regionalStructure", [])
                municipality = next(
                    (
                        part.get("name", "")
                        for part in regional
                        if part.get("type") == "regional.municipality"
                    ),
                    "",
                )
                country = next(
                    (
                        part.get("isoCode", "")
                        for part in regional
                        if part.get("type") == "regional.country"
                    ),
                    "",
                )
                position = entity.get("position", {})
                if "lat" not in position or "lon" not in position:
                    continue
                label_text = entity.get("label", "")
                label = normalize_text(label_text)
                stop_labels = (
                    "zastav",
                    "station",
                    "stop",
                    "haltestelle",
                    "bahnhof",
                    "przystanek",
                    "stanica",
                )
                kind = "stop" if any(value in label for value in stop_labels) else "poi"
                candidates.append(
                    Candidate(
                        (entity.get("name", ""),),
                        float(position["lat"]),
                        float(position["lon"]),
                        municipality,
                        country,
                        kind,
                        region=region_for_coordinates(
                            float(position["lat"]), float(position["lon"])
                        )
                        or "",
                        source="mapy",
                        rank=rank,
                        label=label_text,
                        query=query,
                    )
                )
        return candidates


def run_osm(args) -> None:
    client = OverpassClient(endpoint=args.overpass_url, retries=args.retries)
    candidates = []
    failures = []
    for bbox in args.bbox:
        try:
            candidates.extend(osm_candidates(client.extract(bbox, args.cache)))
        except RuntimeError as error:
            failures.append(bbox)
            print(f"Overpass batch failed for {bbox}: {error}", file=sys.stderr)
    if not candidates and failures:
        raise RuntimeError(f"All {len(failures)} Overpass batches failed")
    resolve(args, lambda _stop: candidates)
    if failures:
        print(json.dumps({"failed_bboxes": failures}, sort_keys=True), file=sys.stderr)


def run_osm_auto(args) -> None:
    queries = load_queries(args.input)
    overpass = OverpassClient(endpoint=args.overpass_url, retries=args.retries)
    nominatim = NominatimClient(retries=args.retries)
    known_centers = (
        gtfs_municipality_centers(args.gtfs, args.jdf)
        if args.gtfs is not None and args.jdf is not None
        else {}
    )
    candidates_by_municipality: dict[tuple[str, str, str], list[Candidate]] = {}
    failures = []
    for stop in queries:
        country = normalize_country_code(stop.country) or "CZ"
        key = country, normalize_text(stop.municipality), stop.region
        if key in candidates_by_municipality:
            continue
        position = known_centers.get(key)
        if position is None:
            same_name_centers = [
                center
                for (center_country, center_name, _), center in known_centers.items()
                if center_country == country
                and center_name == normalize_text(stop.municipality)
            ]
            if len(same_name_centers) == 1:
                position = same_name_centers[0]
        if position is None:
            position = nominatim.municipality(
                stop.municipality, country, args.nominatim_cache
            )
        if position is None:
            candidates_by_municipality[key] = []
            failures.append(
                {
                    "municipality": stop.municipality,
                    "country": country,
                    "reason": "center_not_found",
                }
            )
            continue
        bbox = municipality_bbox(position, args.radius_degrees)
        try:
            elements = overpass.extract(bbox, args.cache, cached_only=args.cached_only)
            if elements is None:
                candidates_by_municipality[key] = []
                failures.append(
                    {
                        "municipality": stop.municipality,
                        "country": country,
                        "reason": "cache_miss",
                    }
                )
                continue
            candidates_by_municipality[key] = osm_candidates(
                elements,
                fallback_municipality=stop.municipality,
                fallback_country=country,
            )
        except RuntimeError:
            candidates_by_municipality[key] = []
            failures.append(
                {
                    "municipality": stop.municipality,
                    "country": country,
                    "reason": "overpass_failed",
                }
            )
        if args.overpass_delay:
            time.sleep(args.overpass_delay)

    resolve(
        args,
        lambda stop: candidates_by_municipality.get(
            (
                normalize_country_code(stop.country) or "CZ",
                normalize_text(stop.municipality),
                stop.region,
            ),
            [],
        ),
    )
    if failures:
        print(
            json.dumps(
                {"municipality_failures": failures}, ensure_ascii=False, sort_keys=True
            ),
            file=sys.stderr,
        )


def run_mapy(args) -> None:
    key = os.environ.get("MAPY_API_KEY", "")
    centers = (
        gtfs_municipality_centers(args.gtfs, args.jdf)
        if args.gtfs is not None and args.jdf is not None
        else {}
    )
    client = MapyClient(key, municipality_centers=centers, retries=args.retries)
    resolve(args, client.geocode)


def run_osm_search(args) -> None:
    client = NominatimClient(retries=args.retries, delay=max(1.0, args.delay))
    resolve(args, lambda stop: client.search(stop, args.cache))


def geodata_row_key(row: list[str]) -> tuple[str, str, str]:
    return (
        normalize_text(row[0]),
        row[3].strip(),
        normalize_country_code(row[4]) or "CZ",
    )


def run_merge(args) -> None:
    def load(path: Path) -> list[list[str]]:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = [row for row in csv.reader(stream) if row]
        if any(len(row) != 5 for row in rows):
            raise ValueError(f"{path} contains an invalid geodata row")
        return rows

    primary = load(args.primary)
    supplement = load(args.supplement)
    merged = {geodata_row_key(row): row for row in primary}
    added = 0
    conflicts = 0
    for row in supplement:
        key = geodata_row_key(row)
        if key not in merged:
            merged[key] = row
            added += 1
        elif merged[key][1:3] != row[1:3]:
            conflicts += 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(
            sorted(merged.values(), key=geodata_row_key)
        )

    residual = None
    if args.review_input and args.review_output:
        with args.review_input.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = reader.fieldnames
            review_rows = list(reader)
        accepted_keys = set(merged)
        remaining = [
            row
            for row in review_rows
            if (
                normalize_text(row["name"]),
                row["region"].strip(),
                normalize_country_code(row["country"]) or "CZ",
            )
            not in accepted_keys
        ]
        args.review_output.parent.mkdir(parents=True, exist_ok=True)
        with args.review_output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(remaining)
        residual = len(remaining)
    print(
        json.dumps(
            {
                "primary": len(primary),
                "supplement": len(supplement),
                "added": added,
                "conflicts_kept_primary": conflicts,
                "merged": len(merged),
                "residual": residual,
            },
            sort_keys=True,
        )
    )


def resolve(args, candidate_provider) -> None:
    accepted = []
    review = []
    candidate_review = []
    queries = load_queries(args.input)
    for stop in queries:
        candidates = candidate_provider(stop)
        assessments = [
            (candidate, *candidate_geography(stop, candidate))
            for candidate in candidates
        ]
        plausible = [candidate for candidate, accepted, _, _ in assessments if accepted]
        chosen = choose_exact_candidate(stop, plausible)
        fuzzy_threshold = getattr(args, "fuzzy_threshold", 0.0)
        if chosen is None and fuzzy_threshold:
            chosen = choose_fuzzy_candidate(stop, plausible, fuzzy_threshold)
        if chosen:
            accepted.append((stop, chosen))
        else:
            if not candidates:
                reason = "not_found"
            elif not plausible:
                reason = "geography_rejected"
            else:
                reason = "ambiguous_or_weak_name"
            review.append((stop, reason, len(candidates)))
            candidate_review.extend((stop, *assessment) for assessment in assessments)
        if args.delay:
            time.sleep(args.delay)
    write_results(args.output, args.review, accepted, review)
    candidate_review_path = getattr(args, "candidate_review", None)
    if candidate_review_path is not None:
        candidate_review_path.parent.mkdir(parents=True, exist_ok=True)
        with candidate_review_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(
                [
                    "stop_id",
                    "query_name",
                    "candidate_names",
                    "query",
                    "label",
                    "latitude",
                    "longitude",
                    "distance_from_estimate_metres",
                    "municipality",
                    "country",
                    "kind",
                    "source",
                    "rank",
                    "name_score",
                    "route_feasible",
                    "rejection_reason",
                ]
            )
            for (
                stop,
                candidate,
                feasible,
                reason,
                distance_from_estimate,
            ) in candidate_review:
                writer.writerow(
                    [
                        stop.stop_id,
                        stop.name,
                        " | ".join(candidate.names),
                        candidate.query,
                        candidate.label,
                        candidate.latitude,
                        candidate.longitude,
                        round(distance_from_estimate, 1)
                        if distance_from_estimate is not None
                        else "",
                        candidate.municipality,
                        candidate.country,
                        candidate.kind,
                        candidate.source,
                        candidate.rank,
                        round(candidate_name_score(stop, candidate), 6),
                        feasible,
                        "" if feasible else reason,
                    ]
                )
    print(
        json.dumps({"accepted": len(accepted), "review": len(review)}, sort_keys=True)
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("gtfs", type=Path)
    audit.add_argument("jdf", type=Path)
    audit.add_argument("output", type=Path)
    audit.add_argument(
        "--coordinate-status",
        choices=("missing", "estimated", "all"),
        default="missing",
        help="select bundle coordinate precision values to audit",
    )
    audit.add_argument(
        "--metadata",
        type=Path,
        help="source_stop_metadata.parquet (defaults to the parent of the GTFS directory)",
    )
    audit.set_defaults(handler=run_audit)
    merge = commands.add_parser("merge")
    merge.add_argument("primary", type=Path)
    merge.add_argument("supplement", type=Path)
    merge.add_argument("output", type=Path)
    merge.add_argument("--review-input", type=Path)
    merge.add_argument("--review-output", type=Path)
    merge.set_defaults(handler=run_merge)
    for name, handler in (
        ("osm", run_osm),
        ("osm-auto", run_osm_auto),
        ("osm-search", run_osm_search),
        ("mapy", run_mapy),
    ):
        command = commands.add_parser(name)
        command.add_argument("input", type=Path)
        command.add_argument("output", type=Path)
        command.add_argument("--review", type=Path, required=True)
        command.add_argument("--retries", type=int, default=3)
        command.add_argument(
            "--delay",
            type=float,
            default=0.25 if name == "mapy" else (1.0 if name == "osm-search" else 0.0),
        )
        if name == "mapy":
            command.add_argument("--gtfs", type=Path)
            command.add_argument("--jdf", type=Path)
            command.add_argument("--fuzzy-threshold", type=float, default=0.7)
            command.add_argument("--candidate-review", type=Path)
        command.set_defaults(handler=handler)
        if name == "osm":
            command.add_argument(
                "--bbox", action="append", required=True, help="south,west,north,east"
            )
            command.add_argument("--cache", type=Path, required=True)
            command.add_argument("--overpass-url", default=OVERPASS_URL)
        elif name == "osm-auto":
            command.add_argument("--cache", type=Path, required=True)
            command.add_argument("--nominatim-cache", type=Path, required=True)
            command.add_argument("--overpass-url", default=OVERPASS_URL)
            command.add_argument("--radius-degrees", type=float, default=0.12)
            command.add_argument("--overpass-delay", type=float, default=0.5)
            command.add_argument("--gtfs", type=Path)
            command.add_argument("--jdf", type=Path)
            command.add_argument("--cached-only", action="store_true")
        elif name == "osm-search":
            command.add_argument("--cache", type=Path, required=True)
    return root


if __name__ == "__main__":
    args = parser().parse_args()
    args.handler(args)
