#!/usr/bin/env python3
"""Conservative one-shot OSM and Mapy coordinate gap filling.

The input is a UTF-8 CSV with ``stop_id,name,municipality,region,country``.
Only unique exact normalized identities are accepted automatically. Everything
else is written to a review CSV; raw service responses are cached only for OSM
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


ABBREVIATIONS = {
    "abzw": "abzweigung",
    "aut nadr": "autobusove nadrazi",
    "aut st": "autobusove stanoviste",
    "cint": "cintorin",
    "dw aut": "dworzec autobusowy",
    "hl nadr": "hlavni nadrazi",
    "kriz": "krizovatka",
    "nam": "namesti",
    "nem": "nemocnice",
    "razc": "razcestie",
    "zel st": "zeleznicni stanice",
}

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
    value = value.casefold().translate(str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss"}))
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
    if stop.municipality and components and normalize_text(components[0]) == normalize_text(
        stop.municipality
    ):
        identities.add(normalize_text(" ".join(components[1:])))
    if stop.municipality and (
        not components or normalize_text(components[0]) != normalize_text(stop.municipality)
    ):
        identities.add(normalize_text(f"{stop.municipality},{stop.name}"))
    return identities - {""}


def identity_variants(value: str) -> set[str]:
    normalized = normalize_text(value)
    return {normalized, " ".join(sorted(normalized.split()))} - {""}


def normalized_municipality(value: str) -> str:
    normalized = normalize_text(value)
    return MUNICIPALITY_ALIASES.get(normalized, normalized)


def _country_matches(expected: str, actual: str) -> bool:
    expected = normalize_country_code(expected) or "CZ"
    actual = normalize_country_code(actual) or "CZ"
    return expected == actual


def distance_metres(left: Candidate, right: Candidate) -> float:
    latitude = math.radians((left.latitude + right.latitude) / 2)
    north = (left.latitude - right.latitude) * 111_320
    east = (left.longitude - right.longitude) * 111_320 * math.cos(latitude)
    return math.hypot(north, east)


def choose_exact_candidate(stop: StopQuery, candidates: list[Candidate]) -> Candidate | None:
    identities = {variant for name in stop_name_identities(stop) for variant in identity_variants(name)}
    municipality = normalized_municipality(stop.municipality or stop.name.split(",", 1)[0])
    accepted = []
    for candidate in candidates:
        if candidate.kind != "stop":
            continue
        if not _country_matches(stop.country, candidate.country):
            continue
        if municipality:
            if candidate.municipality:
                if normalized_municipality(candidate.municipality) != municipality:
                    continue
            elif not stop.region or candidate.region != stop.region:
                continue
        candidate_identities = {
            variant for name in candidate.names for variant in identity_variants(name)
        }
        if not identities.intersection(candidate_identities):
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
        matching = [cluster for cluster in clusters if any(distance_metres(position, item) <= 500 for item in cluster)]
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


def choose_town_candidate(stop: StopQuery, candidates: list[Candidate]) -> Candidate | None:
    municipality = normalized_municipality(stop.municipality or stop.name.split(",", 1)[0])
    accepted = [
        candidate
        for candidate in candidates
        if candidate.kind == "town"
        and _country_matches(stop.country, candidate.country)
        and normalized_municipality(candidate.names[0]) == municipality
    ]
    unique_positions = {(item.latitude, item.longitude): item for item in accepted}
    if len(unique_positions) == 1:
        return next(iter(unique_positions.values()))
    same_region = [
        item for item in unique_positions.values() if stop.region and item.region == stop.region
    ]
    if len(same_region) == 1:
        return same_region[0]
    bounded_first = [item for item in accepted if item.source == "mapy-town-bbox" and item.rank == 0]
    return bounded_first[0] if len(bounded_first) == 1 else None


def choose_fuzzy_candidate(
    stop: StopQuery, candidates: list[Candidate], threshold: float
) -> Candidate | None:
    municipality = normalized_municipality(stop.municipality or stop.name.split(",", 1)[0])
    targets = stop_name_identities(stop)
    scored_positions: dict[tuple[float, float], tuple[float, Candidate]] = {}
    for candidate in candidates:
        if candidate.kind != "stop" or not _country_matches(stop.country, candidate.country):
            continue
        if candidate.municipality and normalized_municipality(candidate.municipality) != municipality:
            continue
        score = max(
            difflib.SequenceMatcher(None, normalize_text(target), normalize_text(name)).ratio()
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
        return [
            StopQuery(
                (row.get("stop_id") or row.get("stop_ids") or "").strip(),
                row["name"].strip(),
                row["municipality"].strip(),
                row["region"].strip(),
                row["country"].strip(),
            )
            for row in rows
        ]


def apply_context_overrides(queries: list[StopQuery], path: Path | None) -> list[StopQuery]:
    if path is None:
        return queries
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream)
        required = {"stop_id", "municipality"}
        if not rows.fieldnames or not required.issubset(rows.fieldnames):
            raise ValueError(f"{path} must contain stop_id and municipality")
        overrides = {
            row["stop_id"].strip(): row["municipality"].strip()
            for row in rows
            if row["stop_id"].strip() and row["municipality"].strip()
        }
    return [
        StopQuery(
            query.stop_id,
            query.name,
            overrides.get(query.stop_id, query.municipality),
            query.region,
            query.country,
        )
        for query in queries
    ]


def run_audit(args) -> None:
    with (args.gtfs / "stop_times.txt").open(encoding="utf-8-sig", newline="") as stream:
        referenced = {row["stop_id"] for row in csv.DictReader(stream)}
    with (args.gtfs / "stops.txt").open(encoding="utf-8-sig", newline="") as stream:
        stops = list(csv.DictReader(stream))
    unresolved = {
        row["stop_id"]: row
        for row in stops
        if row["stop_id"] in referenced
        and float(row["stop_lat"]) == 0
        and float(row["stop_lon"]) == 0
    }
    unreferenced = [
        row for row in stops if row.get("location_type", "") != "1" and row["stop_id"] not in referenced
    ]
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
            fieldnames=["stop_id", "name", "municipality", "region", "country"],
            lineterminator="\n",
        )
        writer.writeheader()
        for stop_id in sorted(unresolved):
            match = re.match(r"jdf:stop:(\d+)", stop_id)
            if not match or match.group(1) not in source_stops:
                raise ValueError(f"Cannot join GTFS stop to merged JDF: {stop_id}")
            source = source_stops[match.group(1)]
            writer.writerow(
                {
                    "stop_id": stop_id,
                    "name": source.name,
                    "municipality": source.municipality,
                    "region": source.region,
                    "country": source.country,
                }
            )
    print(
        json.dumps(
            {
                "emitted_stops": len(stops),
                "referenced_boarding_stops": len(referenced),
                "unreferenced_boarding_stops": len(unreferenced),
                "referenced_missing_coordinates": len(unresolved),
                "referenced_with_coordinates": len(referenced) - len(unresolved),
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
                    "S" if candidate.kind == "stop" else "T",
                ]
            )
    with review_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            ["stop_id", "name", "municipality", "region", "country", "reason", "candidates"]
        )
        for stop, reason, count in sorted(review, key=lambda item: item[0].stop_id):
            writer.writerow(
                [
                    stop.stop_id,
                    stop.name,
                    stop.municipality,
                    stop.region,
                    stop.country,
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
        raise RuntimeError(f"Request failed after {self.retries} attempts") from last_error


class OverpassClient(JsonHttpClient):
    def __init__(self, endpoint: str = OVERPASS_URL, **kwargs):
        super().__init__(**kwargs)
        self.endpoint = endpoint

    def extract(self, bbox: str, cache: Path, cached_only: bool = False) -> list[dict] | None:
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
        query = "[out:json][timeout:180];(" + "".join(
            f"nwr{selector}({bbox});" for selector in selectors
        ) + ");out tags center;"
        payload = self.request(
            "POST", self.endpoint, data={"data": query}, headers={"User-Agent": USER_AGENT}
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
            names.extend(part.strip() for part in tags["alt_name"].split(";") if part.strip())
        position = element.get("center", element)
        if not names or "lat" not in position or "lon" not in position:
            continue
        municipality = next(
            (tags.get(key, "") for key in ("addr:city", "is_in:city", "is_in") if tags.get(key)),
            fallback_municipality,
        )
        country = tags.get("addr:country", tags.get("is_in:country_code", fallback_country))
        result.append(
            Candidate(
                tuple(names),
                float(position["lat"]),
                float(position["lon"]),
                municipality,
                country,
                region=region_for_coordinates(float(position["lat"]), float(position["lon"])) or "",
            )
        )
    return result


class NominatimClient(JsonHttpClient):
    def municipality(self, name: str, country: str, cache: Path) -> tuple[float, float] | None:
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
        return (float(payload[0]["lat"]), float(payload[0]["lon"])) if len(payload) == 1 else None

    def search(self, stop: StopQuery, cache: Path) -> list[Candidate]:
        cache.mkdir(parents=True, exist_ok=True)
        key = normalize_text(f"stop-{stop.country}-{stop.name}-{stop.municipality}").replace(
            " ", "-"
        )
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
                    "countrycodes": (iso_country_code(stop.country) or stop.country).lower(),
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
            is_stop = category in {"highway", "public_transport", "railway"} or result_type in {
                "bus_stop",
                "platform",
                "station",
                "halt",
                "tram_stop",
            }
            is_town = category in {"place", "boundary"} and result_type in {
                "city",
                "town",
                "village",
                "municipality",
                "hamlet",
                "administrative",
            }
            candidates.append(
                Candidate(
                    names,
                    float(item["lat"]),
                    float(item["lon"]),
                    municipality,
                    address.get("country_code", stop.country),
                    "stop" if is_stop else "town" if is_town else "poi",
                    region=region_for_coordinates(float(item["lat"]), float(item["lon"])) or "",
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


def foreign_town_candidates(
    stop: StopQuery,
    client: NominatimClient,
    cache: Path,
    memo: dict[tuple[str, str], list[Candidate]],
) -> list[Candidate]:
    country = normalize_country_code(stop.country) or "CZ"
    municipality = stop.municipality.strip()
    if country == "CZ" or not municipality:
        return []
    key = (country, normalize_text(municipality))
    if key not in memo:
        position = client.municipality(municipality, country, cache)
        memo[key] = (
            [Candidate((municipality,), *position, municipality, country, "town")]
            if position is not None
            else []
        )
    return memo[key]


class MapyClient(JsonHttpClient):
    def __init__(self, api_key: str, municipality_centers=None, **kwargs):
        super().__init__(**kwargs)
        if not api_key:
            raise ValueError("MAPY_API_KEY is required")
        self.api_key = api_key
        self.municipality_centers = municipality_centers or {}

    def municipality_center(self, stop: StopQuery) -> tuple[float, float] | None:
        country = normalize_country_code(stop.country) or "CZ"
        name = normalize_text(stop.municipality)
        exact = self.municipality_centers.get((country, name, stop.region))
        if exact is not None:
            return exact
        matches = [
            center
            for (center_country, center_name, _), center in self.municipality_centers.items()
            if center_country == country and center_name == name
        ]
        return matches[0] if len(matches) == 1 else None

    def geocode(self, stop: StopQuery) -> list[Candidate]:
        country_locality = iso_country_code(stop.country) or "CZ"
        center = self.municipality_center(stop)
        stop_locality = (
            mapy_bbox(center)
            if center is not None
            else ", ".join(value for value in (stop.municipality, country_locality) if value)
        )
        queries = [(stop.name, "poi", stop_locality)]
        components = [part.strip() for part in stop.name.split(",") if part.strip()]
        if components:
            queries.append(
                (f"{components[-1]}, {stop.municipality or components[0]}", "poi", stop_locality)
            )
        if stop.municipality:
            queries.append(
                (
                    stop.municipality,
                    "regional.municipality",
                    mapy_bbox(center) if center is not None else country_locality,
                )
            )
        candidates = []
        for query, entity_filter, locality in dict.fromkeys(queries):
            payload = self.request(
                "GET",
                MAPY_URL,
                params={
                    "query": query,
                    "lang": "cs",
                    "limit": 5,
                    "type": entity_filter,
                    "locality": locality,
                },
                headers={"X-MAPY-API-KEY": self.api_key, "User-Agent": USER_AGENT},
            )
            for rank, entity in enumerate(payload.get("items", payload.get("entities", []))[:5]):
                regional = entity.get("regionalStructure", [])
                municipality = next(
                    (part.get("name", "") for part in regional if part.get("type") == "regional.municipality"),
                    "",
                )
                country = next(
                    (part.get("isoCode", "") for part in regional if part.get("type") == "regional.country"),
                    "",
                )
                position = entity.get("position", {})
                if "lat" not in position or "lon" not in position:
                    continue
                entity_type = entity.get("type", "")
                label = normalize_text(entity.get("label", ""))
                stop_labels = (
                    "zastav",
                    "station",
                    "stop",
                    "haltestelle",
                    "bahnhof",
                    "przystanek",
                    "stanica",
                )
                kind = (
                    "town"
                    if entity_type == "regional.municipality"
                    else "stop"
                    if any(value in label for value in stop_labels)
                    else "poi"
                )
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
                        source=(
                            "mapy-town-bbox"
                            if entity_filter == "regional.municipality" and center is not None
                            else "mapy"
                        ),
                        rank=rank,
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
    nominatim = NominatimClient(retries=args.retries)
    town_memo: dict[tuple[str, str], list[Candidate]] = {}
    resolve(
        args,
        lambda _stop: candidates,
        town_provider=lambda stop: foreign_town_candidates(
            stop, nominatim, args.nominatim_cache, town_memo
        ),
    )
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
                if center_country == country and center_name == normalize_text(stop.municipality)
            ]
            if len(same_name_centers) == 1:
                position = same_name_centers[0]
        if position is None:
            position = nominatim.municipality(stop.municipality, country, args.nominatim_cache)
        if position is None:
            candidates_by_municipality[key] = []
            failures.append({"municipality": stop.municipality, "country": country, "reason": "center_not_found"})
            continue
        bbox = municipality_bbox(position, args.radius_degrees)
        try:
            elements = overpass.extract(bbox, args.cache, cached_only=args.cached_only)
            if elements is None:
                candidates_by_municipality[key] = []
                failures.append(
                    {"municipality": stop.municipality, "country": country, "reason": "cache_miss"}
                )
                continue
            candidates_by_municipality[key] = osm_candidates(
                elements,
                fallback_municipality=stop.municipality,
                fallback_country=country,
            )
        except RuntimeError:
            candidates_by_municipality[key] = []
            failures.append({"municipality": stop.municipality, "country": country, "reason": "overpass_failed"})
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
        print(json.dumps({"municipality_failures": failures}, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def run_mapy(args) -> None:
    key = os.environ.get("MAPY_API_KEY", "")
    centers = (
        gtfs_municipality_centers(args.gtfs, args.jdf)
        if args.gtfs is not None and args.jdf is not None
        else {}
    )
    client = MapyClient(key, municipality_centers=centers, retries=args.retries)
    resolve(args, client.geocode, allow_town=True)


def run_osm_search(args) -> None:
    client = NominatimClient(retries=args.retries, delay=max(1.0, args.delay))
    resolve(args, lambda stop: client.search(stop, args.cache), allow_town=True)


def geodata_row_key(row: list[str]) -> tuple[str, str, str]:
    return normalize_text(row[0]), row[3].strip(), normalize_country_code(row[4]) or "CZ"


def run_merge(args) -> None:
    def load(path: Path) -> list[list[str]]:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = [row for row in csv.reader(stream) if row]
        if any(len(row) not in {5, 6} for row in rows):
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


def resolve(args, candidate_provider, allow_town: bool = False, town_provider=None) -> None:
    accepted = []
    review = []
    candidate_review = []
    queries = apply_context_overrides(
        load_queries(args.input), getattr(args, "context_overrides", None)
    )
    for stop in queries:
        candidates = candidate_provider(stop)
        chosen = choose_exact_candidate(stop, candidates)
        fuzzy_threshold = getattr(args, "fuzzy_threshold", 0.0)
        if chosen is None and fuzzy_threshold:
            chosen = choose_fuzzy_candidate(stop, candidates, fuzzy_threshold)
        if chosen is None and allow_town:
            chosen = choose_town_candidate(stop, candidates)
        if chosen is None and town_provider is not None:
            town_candidates = town_provider(stop)
            candidates.extend(town_candidates)
            chosen = choose_town_candidate(stop, town_candidates)
        if chosen:
            accepted.append((stop, chosen))
        else:
            review.append((stop, "ambiguous" if candidates else "not_found", len(candidates)))
            candidate_review.extend((stop, candidate) for candidate in candidates)
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
                    "latitude",
                    "longitude",
                    "municipality",
                    "country",
                    "kind",
                    "source",
                    "rank",
                ]
            )
            for stop, candidate in candidate_review:
                writer.writerow(
                    [
                        stop.stop_id,
                        stop.name,
                        " | ".join(candidate.names),
                        candidate.latitude,
                        candidate.longitude,
                        candidate.municipality,
                        candidate.country,
                        candidate.kind,
                        candidate.source,
                        candidate.rank,
                    ]
                )
    print(json.dumps({"accepted": len(accepted), "review": len(review)}, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("gtfs", type=Path)
    audit.add_argument("jdf", type=Path)
    audit.add_argument("output", type=Path)
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
            "--delay", type=float, default=0.25 if name == "mapy" else (1.0 if name == "osm-search" else 0.0)
        )
        if name == "mapy":
            command.add_argument("--gtfs", type=Path)
            command.add_argument("--jdf", type=Path)
            command.add_argument("--fuzzy-threshold", type=float, default=0.0)
            command.add_argument("--candidate-review", type=Path)
        if name in {"mapy", "osm-search"}:
            command.add_argument("--context-overrides", type=Path)
        command.set_defaults(handler=handler)
        if name == "osm":
            command.add_argument("--bbox", action="append", required=True, help="south,west,north,east")
            command.add_argument("--cache", type=Path, required=True)
            command.add_argument("--nominatim-cache", type=Path, required=True)
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
