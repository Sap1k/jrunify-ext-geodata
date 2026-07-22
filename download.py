#!/usr/bin/env python3
import io
import re
import ssl
import sys
import csv
import gzip
import json
import math
import time
import pathlib
import urllib3
import zipfile
import itertools
from dataclasses import dataclass
import lxml.html
import pyproj
import shapely
import shapely.geometry
import requests
import requests.adapters
import shapefile


@dataclass
class Stop:
    name: str
    lat: float
    lon: float
    region: str | None = None
    country: str | None = None


# JDF country identifiers intentionally retain historical values. External
# sources generally publish ISO 3166-1 alpha-2 codes, so translate them once
# while materialising the geodata repository rather than changing JDF data.
COUNTRY_CODES_TO_JDF = {
    "AT": "A",
    "BE": "B",
    "BA": "BA",
    "BY": "BY",
    "CH": "CH",
    "CZ": "CZ",
    "DE": "D",
    "DK": "DK",
    "EE": "EST",
    "ES": "E",
    "FR": "F",
    "GB": "GB",
    "GR": "GR",
    "HR": "HR",
    "HU": "H",
    "IT": "I",
    "LI": "FL",
    "LT": "LT",
    "LU": "L",
    "LV": "LV",
    "MD": "MD",
    "ME": "MNE",
    "MK": "MK",
    "NL": "NL",
    "NO": "N",
    "PL": "PL",
    "RO": "RO",
    "RS": "SRB",
    "SE": "S",
    "SI": "SLO",
    "SK": "SK",
    "TR": "TR",
    "UA": "UA",
}
for jdf_country_code in tuple(COUNTRY_CODES_TO_JDF.values()):
    COUNTRY_CODES_TO_JDF.setdefault(jdf_country_code, jdf_country_code)


def normalize_country_code(country):
    if not country:
        return None
    country = country.strip().upper()
    normalized = COUNTRY_CODES_TO_JDF.get(country)
    if normalized is None:
        print(
            f"Unknown country code {country!r}; leaving it unchanged", file=sys.stderr
        )
        return country
    return normalized


JDF_COUNTRY_CODES_TO_ISO = {
    "A": "AT",
    "B": "BE",
    "D": "DE",
    "E": "ES",
    "EST": "EE",
    "F": "FR",
    "FL": "LI",
    "H": "HU",
    "I": "IT",
    "L": "LU",
    "MNE": "ME",
    "N": "NO",
    "S": "SE",
    "SLO": "SI",
    "SRB": "RS",
}


def iso_country_code(country):
    normalized = normalize_country_code(country)
    return JDF_COUNTRY_CODES_TO_ISO.get(normalized, normalized)


COUNTRY_OVERRIDES = {
    # These border crossing stops sometimes end up on the wrong side of a
    # simplified border line, for now just fix them manually.
    "Drasenhofen,,ZOLL": (None, "AT"),
    "Reitzenhain,Wendeschleife": (None, "DE"),
    "Reitzenhain,ZOLL": (None, "DE"),
    "Wullowitz,,ZOLL": (None, "AT"),
}


# See https://stackoverflow.com/a/73519818
class LegacyHttpAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, **kwargs):
        self.ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        self.ssl_context.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = urllib3.poolmanager.PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_context=self.ssl_context,
        )


legacy_session = requests.session()
legacy_session.mount("https://", LegacyHttpAdapter())


# towns.json and regions.json are derived from data by ČÚZK:
# https://geoportal.cuzk.cz/Default.aspx?mode=TextMeta&side=dSady_RUIAN_vse&metadataID=CZ-00025712-CUZK_SERIES-MD_RUIAN-STATY-SHP&head_tab=sekce-02-gp&menu=3327
def load_towns():
    towns_file = pathlib.Path(__file__).parent / "data" / "towns.json.gz"
    towns = {}
    with gzip.open(towns_file) as f:
        for feat in json.load(f)["features"]:
            name = feat["properties"]["name"]
            shape = shapely.geometry.shape(feat["geometry"])
            towns[name] = shape
    return towns


towns = load_towns()


def load_polygon_tree(path, code_attr):
    features = []
    feature_codes = []
    with gzip.open(path) as f:
        for feat in json.load(f)["features"]:
            code = feat["properties"][code_attr]
            feature_codes.append(code)
            shape = shapely.geometry.shape(feat["geometry"])
            features.append(shape)
    return feature_codes, shapely.STRtree(features)


region_codes, regions = load_polygon_tree(
    pathlib.Path(__file__).parent / "data" / "regions.json.gz", "code"
)


def region_for_coordinates(latitude, longitude):
    point = shapely.Point(longitude, latitude)
    matches = regions.query(point, predicate="within").tolist()
    return region_codes[matches[0]] if len(matches) == 1 else None


# countries.json is derived from data by Natural Earth:
# https://www.naturalearthdata.com/downloads/10m-cultural-vectors/
country_codes, countries = load_polygon_tree(
    pathlib.Path(__file__).parent / "data" / "countries.json.gz", "code"
)


def add_missing_town(stops):
    for stop in stops:
        point = shapely.Point(stop.lon, stop.lat)
        town_added = False
        for town_name, town in towns.items():
            if point.within(town) and f"{town_name}," not in stop.name:
                town_added = True
                yield Stop(f"{town_name}," + stop.name, stop.lat, stop.lon)
        if not town_added:
            yield stop


def add_known_town(stops, town):
    prefix = f"{town},"
    for stop in stops:
        if stop.name.startswith(prefix):
            yield stop
        else:
            yield Stop(
                prefix + stop.name, stop.lat, stop.lon, stop.region, stop.country
            )


def add_missing_regions(stops):
    for stop in stops:
        if stop.region:
            yield stop
            continue
        region = region_for_coordinates(stop.lat, stop.lon)
        if region is not None:
            yield Stop(stop.name, stop.lat, stop.lon, region, "CZ")
        else:
            yield stop


def add_missing_countries(stops):
    for stop in stops:
        if stop.country:
            yield stop
            continue
        point = shapely.Point(stop.lon, stop.lat)
        countries_idx = countries.query(point, predicate="within").tolist()
        if len(countries_idx) == 1:
            yield Stop(
                stop.name,
                stop.lat,
                stop.lon,
                stop.region,
                country_codes[countries_idx[0]],
            )
        else:
            yield stop


def arcgis_download_stops(url, layer, name_fields, where="1=1"):
    if isinstance(layer, list):
        for l in layer:
            for stop in arcgis_download_stops(url, l, name_fields, where):
                yield stop
        return

    batch = 1000
    offset = 0
    while True:
        print("Downloading with offset", offset, file=sys.stderr)
        resp = requests.post(
            f"{url}/{layer}/query",
            data={
                "f": "json",
                "where": where,
                "outSR": "4326",  # WGS84
                "outFields": ",".join(name_fields),
                "resultRecordCount": batch,
                "resultOffset": offset,
            },
        )
        resp.raise_for_status()
        try:
            payload = resp.json()
            if "error" in payload:
                raise RuntimeError(
                    f"ArcGIS error for {url}/{layer}: {payload['error']}"
                )
            stops = payload["features"]
        except Exception:
            print("Failed decoding response:", resp.text)
            return

        for stop in stops:
            attrs = stop["attributes"]
            name = ",".join((attrs[nf] or "").strip() for nf in name_fields)
            geom = stop["geometry"]
            yield Stop(name, geom["y"], geom["x"])

        print("Got", len(stops), "stops", file=sys.stderr)
        if len(stops) != batch:
            break
        offset += batch


def abirun_tim_download_stops(url):
    sess = requests.session()
    resp = sess.get(url)
    homepage = lxml.html.fromstring(resp.content)
    tariff_id = homepage.xpath("//select[@id='tarifValidity']/option/@value")[0]

    resp = sess.post(f"{url}/ZakladniDataMapy", json={"platnostTarifuId": tariff_id})
    zones = resp.json()["zones"]

    for zone in zones:
        time.sleep(0.1)
        resp = sess.post(
            f"{url}/Zastavky",
            json={"platnostTarifuId": tariff_id, "zonaId": zone["id"]},
        )
        for stop in resp.json():
            if not stop["isBus"]:
                continue
            yield Stop(stop["text"], stop["point"]["lat"], stop["point"]["lon"])


def mapaduk_download_stops():
    URL = "https://provoz.kr-ustecky.cz/TMD/API/Map/GetStopMarkers"

    resp = requests.post(URL, json={})
    stops = resp.json()["ItemL"]

    # Sometimes there are multiple entries for stops, and only one is
    # identified correctly as a train stop
    for (lat, lng), stops in itertools.groupby(stops, lambda s: (s["Lat"], s["Lng"])):
        stops = list(stops)
        # These look like train stops
        if any(s["PostNote"] in ["žst.", "žel.zast."] for s in stops):
            continue
        for stop in stops:
            yield Stop(stop["Name"], lat, lng)


def qride_download_stops():
    resp = requests.get("https://tabule.portabo.cz/api/v1-tabule/cis/GetStations")
    for stop in resp.json()["ItemList"]:
        if stop["Latitude"] is None or stop["Longitude"] is None:
            print(f"Skipping stop {stop['Name']} without position")
            continue
        yield Stop(stop["Name"], stop["Latitude"], stop["Longitude"])


def iredo_mapa2_download_stops():
    resp = requests.post(
        "https://iredo.online/map/mapData",
        json={"w": 0, "s": 0, "e": 180, "n": 180, "zoom": 20},
    )
    for stop in resp.json()["stops"]:
        if stop["sourceType"] != "S":
            continue
        yield Stop(stop["name"], stop["lat"], stop["lon"])


def tmapy_download_stops(url):
    resp = requests.get(url + "/idspublicservices/api/station")
    stops = resp.json()

    for stop in stops:
        if "lat" not in stop:
            continue
        if all(st["vehicleType"] == "V" for st in stop["serviceTypes"]):
            # Skip train-only stops
            continue
        yield Stop(stop["name"], stop["lat"], stop["lon"])


def mpvnet_download_stops(instance):
    URL = "https://mpvnet.cz"
    # Whole Czech Rep.
    BBOX = [48.195, 12.000, 51.385, 18.951]

    resp = requests.post(
        f"{URL}/{instance}/map/mapData",
        json={
            "s": BBOX[0],
            "w": BBOX[1],
            "n": BBOX[2],
            "e": BBOX[3],
            "zoom": 20,
            "showStops": True,
        },
        headers={
            "Origin": "https://mpvnet.cz",
        },
    )
    for stop in resp.json()["stops"]:
        # Filter out train stops
        if stop["t"] == "T":
            continue
        yield Stop(stop["n"], stop["x"], stop["y"])


def zipped_geojson_download_stops(url, name_prop, pre_urls=[]):
    def geom_to_points(geom):
        if geom["type"] == "Polygon":
            shape = shapely.geometry.shape(geom)
            centre = shape.centroid
            yield (centre.y, centre.x)
        elif geom["type"] == "Point":
            yield (geom["coordinates"][1], geom["coordinates"][0])
        elif geom["type"] == "GeometryCollection":
            for g in geom["geometries"]:
                for p in geom_to_points(g):
                    yield p
        else:
            assert False, f"Unknown geometry type: {geom['type']}"

    sess = requests.session()
    for pre_url in pre_urls:
        sess.get(pre_url)
    resp = sess.get(url)
    zip_io = io.BytesIO(resp.content)
    zip = zipfile.ZipFile(zip_io)
    with zip.open(zip.namelist()[0]) as f:
        geojson = json.load(f)
        for feat in geojson["features"]:
            for lat, lon in geom_to_points(feat["geometry"]):
                yield Stop(feat["properties"][name_prop], lat, lon)


def jihocesky_kraj_download_stops():
    URL = "https://geoportal.kraj-jihocesky.gov.cz/portal/media/Soubory/opendata/zastavky_JCK_SHP.zip"
    zip_resp = requests.get(URL)
    zip_io = io.BytesIO(zip_resp.content)
    zip = zipfile.ZipFile(zip_io)

    shp_name = next(n for n in zip.namelist() if n.endswith(".shp"))
    dbf_name = next(n for n in zip.namelist() if n.endswith(".dbf"))

    transformer = pyproj.Transformer.from_crs(5514, 4326)  # Křovák -> WGS 84

    with (
        zip.open(shp_name) as shp_file,
        zip.open(dbf_name) as dbf_file,
        shapefile.Reader(shp=shp_file, dbf=dbf_file, encoding="UTF-8") as shp,
    ):
        stops = []
        for shrec in shp.shapeRecords():
            if shrec.record["TYP"] == "vlak":
                continue
            lat, lon = transformer.transform(*shrec.shape.points[0])
            name = shrec.record["POPIS_LONG"]
            stops.append(Stop(name, lat, lon))
        return stops


def liberecky_kraj_download_stops():
    URL = "https://dopravnimapy.kraj-lbc.cz/opendata/zastavky_shp_wgs84.zip"
    zip_resp = legacy_session.get(URL)
    zip_io = io.BytesIO(zip_resp.content)
    zip = zipfile.ZipFile(zip_io)

    shp_name = next(n for n in zip.namelist() if n.endswith(".shp"))
    dbf_name = next(n for n in zip.namelist() if n.endswith(".dbf"))

    with (
        zip.open(shp_name) as shp_file,
        zip.open(dbf_name) as dbf_file,
        shapefile.Reader(shp=shp_file, dbf=dbf_file, encoding="cp1250") as shp,
    ):
        stops = []
        for shrec in shp.shapeRecords():
            lon, lat = shrec.shape.points[0]
            name = shrec.record["NAZEV"]
            stops.append(Stop(name, lat, lon))
        return stops


def pid_download_stops():
    URL = "https://data.pid.cz/stops/json/stops.json"
    resp = requests.get(URL)
    for group in resp.json()["stopGroups"]:
        for stop in group["stops"]:
            if all(l["type"] == "train" for l in stop["lines"]):
                continue
            lat = stop["lat"]
            lon = stop["lon"]
            yield Stop(group["name"], lat, lon, group["districtCode"])
            if group["municipality"] not in group["name"]:
                yield Stop(
                    group["municipality"] + "," + group["name"],
                    lat,
                    lon,
                    group["districtCode"],
                )


def karlovarsky_kraj_download_stops():
    kv_stops = arcgis_download_stops(
        "https://geoportal.kr-karlovarsky.cz/arcgis/rest/services/UAP/UAP_Kompletni_obsah/MapServer",
        [296, 297, 298],
        ["PrvekNaz"],
    )
    kv_stops_nonum = []
    for stop in kv_stops:
        name = re.sub(r'^("?)[0-9x]* *', r"\1", stop.name)
        name = re.sub(r" *\(.+\)$", "", name)
        name = re.sub(r" +(NÁSTUP|VÝSTUP)$", "", name)
        if name == "":
            continue
        # The source currently labels a point on Hornická street in Chlum
        # Svaté Maří as plain Kaceřov. It is well outside the Kaceřov stop
        # cluster and makes the real stop identity ambiguous.
        if is_known_bad_karlovarsky_point(name, stop.lat, stop.lon):
            continue
        kv_stops_nonum.append(Stop(name, stop.lat, stop.lon))
    return kv_stops_nonum


def is_known_bad_karlovarsky_point(name, lat, lon):
    return name == "Kaceřov" and 50.15 < lat < 50.16 and 12.52 < lon < 12.54


def idsjmk_download_stops():
    stops = arcgis_download_stops(  # URL is backing service for https://data.brno.cz/datasets/747a824783044377b6d07a8060e7769d_0/explore
        "https://services6.arcgis.com/fUWVlHWZNxUvTUh8/ArcGIS/rest/services/stops/FeatureServer",
        0,
        ["stop_name"],
    )
    return add_missing_town(stops)


def most_download_stops():
    stops = arcgis_download_stops(
        # URL is backing service for https://opendata.mesto-most.cz/datasets/e91cb7afcf264116bbcb84d98d30580c_32/explore
        "https://mapy.mesto-most.cz/server/rest/services/Opendata/OpendataProjekty/FeatureServer",
        32,
        ["NAZEV"],
    )
    return add_missing_town(stops)


def ostrava_download_stops():
    return add_missing_town(
        zipped_geojson_download_stops(
            "https://mapy.ostrava.cz/opendata/data/opendata/zastavky_MHD_WGS84_gjson.zip",
            "zast_jm",
        )
    )


def plzen_download_stops():
    return add_missing_town(
        zipped_geojson_download_stops(
            "https://opendata.plzen.eu/public/opendata/detail/9?detail-fileId=18&do=detail-downloadFile",
            "NAZEV",
            # The website requires some cookies for downloading?
            pre_urls=["https://opendata.plzen.eu/public/opendata/detail/9"],
        )
    )


def zdarns_download_stops():
    resp = requests.get("https://mhdzdar.kdyprijede.cz/stops")
    for stop in resp.json()["stops"]:
        yield Stop(
            "Žďár n.Sáz.," + re.sub(r" *\[.*\]", "", stop[2]),
            stop[4] / 1000000,
            stop[3] / 1000000,
        )


def gtfs_download_stops(url, name_mapper=None):
    response = requests.get(url)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        with archive.open("stops.txt") as raw:
            rows = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            for row in rows:
                lat = row.get("stop_lat", "").strip()
                lon = row.get("stop_lon", "").strip()
                name = row.get("stop_name", "").strip()
                if not name or not lat or not lon:
                    continue
                if name_mapper is not None:
                    name = name_mapper(row, name)
                yield Stop(name, float(lat), float(lon))


def dpmlj_stop_name(row, name):
    """Expand DPMLJ's tariff zones to the municipality components used by JDF."""
    zones = {part.strip() for part in row.get("zone_id", "").split(",")}
    if zones == {"1"}:
        return f"Liberec,,{name}"
    if zones == {"2"}:
        prefix = "Jablonec n.N.,"
        if name.startswith(prefix):
            name = name[len(prefix) :].strip()
        return f"Jablonec n.Nisou,,{name}"
    if zones == {"1", "2"}:
        if name.casefold() == "vratislavická kyselka":
            return f"Liberec,Vratislavice n.Nisou,{name}"
        return f"Jablonec n.Nisou,Proseč n.Nisou,{name}"
    raise ValueError(f"Unknown DPMLJ zone {row.get('zone_id')!r} for {name!r}")


def write_stops_csv(outfile, stops):
    skipped_blank = 0
    raw_stops = stops

    def validated_stops():
        nonlocal skipped_blank
        for stop in raw_stops:
            stop.name = stop.name.strip()
            if not stop.name:
                skipped_blank += 1
                continue
            if (
                not math.isfinite(stop.lat)
                or not math.isfinite(stop.lon)
                or not -90 <= stop.lat <= 90
                or not -180 <= stop.lon <= 180
            ):
                raise ValueError(
                    f"Invalid coordinates for {stop.name!r}: {stop.lat}, {stop.lon}"
                )
            if stop.lat == 0 and stop.lon == 0:
                continue
            yield stop

    stops = add_missing_regions(validated_stops())
    stops = add_missing_countries(stops)
    with outfile.open("w", encoding="utf-8", newline="") as stream:
        w = csv.writer(stream, lineterminator="\n")
        row_count = 0
        seen = set()
        for stop in stops:
            override = COUNTRY_OVERRIDES.get(stop.name)
            if override:
                stop.region = override[0]
                stop.country = override[1]
            country = normalize_country_code(stop.country) or ""
            dedup_key = (
                " ".join(stop.name.casefold().split()),
                stop.region or "",
                country,
                round(stop.lat, 4),
                round(stop.lon, 4),
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            w.writerow(
                [
                    stop.name,
                    stop.lat,
                    stop.lon,
                    stop.region or "",
                    country,
                ]
            )
            row_count += 1
    if row_count == 0:
        raise RuntimeError(f"Source produced no positioned stops: {outfile}")
    if skipped_blank:
        print(f"Skipped {skipped_blank} unnamed positioned rows", file=sys.stderr)
    return row_count


SOURCES = {
    "other/MoravskoslezskyKraj.csv": lambda: arcgis_download_stops(
        # Backing service for https://data.msk.cz/datasets/17da5e4200744a6e8bfd3a8a31777402_0/explore
        "https://services8.arcgis.com/jfWD14yYevYeDEj7/arcgis/rest/services/cp_di_zastavky_vhd/FeatureServer",
        0,
        ["NAZEV_ZASTAVKY"],
    ),
    "other/UsteckyKraj.csv": lambda: arcgis_download_stops(
        "https://ags.kr-ustecky.cz/arcgis/rest/services/Doprava/zastavky/MapServer",
        0,
        ["NAZEV"],
    ),
    "other/IDSJMK.csv": idsjmk_download_stops,
    "other/PlzenskyKraj.csv": lambda: arcgis_download_stops(
        "https://mapy.plzensky-kraj.cz/ArcGIS/rest/services/zastavky/MapServer",
        1,
        ["OZNACENI"],
    ),
    "other/MapaVDV.csv": lambda: abirun_tim_download_stops(
        "https://tim.abirun.eu/KrajVysocina/TarifniPocitadlo/Mapa"
    ),
    "other/MapaDUK.csv": mapaduk_download_stops,
    "other/QRideDUK.csv": qride_download_stops,
    "other/MapaIREDO2.csv": iredo_mapa2_download_stops,
    "other/MapaIDSOK.csv": lambda: tmapy_download_stops("https://cestujok.cz"),
    "other/MPVNet_PID.csv": lambda: mpvnet_download_stops("pid"),
    "other/MPVNet_ODIS.csv": lambda: mpvnet_download_stops("odis"),
    "other/MPVNet_Zlin.csv": lambda: mpvnet_download_stops("zlin"),
    "other/MPVNet_IDOL.csv": lambda: mpvnet_download_stops("idol"),
    "other/MPVNet_JIKORD.csv": lambda: mpvnet_download_stops("jikord"),
    "other/JihoceskyKraj.csv": jihocesky_kraj_download_stops,
    "other/LibereckyKraj.csv": liberecky_kraj_download_stops,
    "other/PID.csv": pid_download_stops,
    "other/Most.csv": most_download_stops,
    "other/Ostrava.csv": ostrava_download_stops,
    "other/Plzen.csv": plzen_download_stops,
    "other/ZdarNS.csv": zdarns_download_stops,
    "other/KralovehradeckyKraj.csv": lambda: arcgis_download_stops(
        "https://services6.arcgis.com/ogJAiK65nXL1mXAW/arcgis/rest/services/Autobusov%C3%A9_zast%C3%A1vky_IREDO/FeatureServer",
        0,
        ["nazev"],
    ),
    "other/DPMLJ.csv": lambda: gtfs_download_stops(
        "https://www.dpmlj.cz/gtfs.zip", dpmlj_stop_name
    ),
    "other/DPMO.csv": lambda: add_known_town(
        gtfs_download_stops("https://www.dpmo.cz/doc/dpmo-olomouc-cz.zip"), "Olomouc"
    ),
}


def download_all(outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    failures = []
    for name, fun in SOURCES.items():
        print(f"-- Downloading {name}", file=sys.stderr)
        out = outdir / name
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_suffix(out.suffix + ".tmp")
        try:
            row_count = write_stops_csv(temporary, fun())
            temporary.replace(out)
            print(f"-- Wrote {row_count} rows to {name}", file=sys.stderr)
        except Exception as error:
            temporary.unlink(missing_ok=True)
            failures.append((name, error))
            print(f"-- Failed {name}: {error}", file=sys.stderr)
    if failures:
        names = ", ".join(name for name, _ in failures)
        raise RuntimeError(f"Failed geodata sources: {names}")


if __name__ == "__main__":
    outdir = pathlib.Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3:
        name = sys.argv[2]
        fun = SOURCES[name]
        out = outdir / name
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_suffix(out.suffix + ".tmp")
        try:
            write_stops_csv(temporary, fun())
            temporary.replace(out)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        download_all(outdir)
