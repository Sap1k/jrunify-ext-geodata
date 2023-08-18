#!/usr/bin/env python3
import io
import re
import ssl
import sys
import csv
import gzip
import json
import time
import pathlib
import urllib3
import zipfile
import itertools
import statistics
import datetime as dt
from dataclasses import dataclass
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

# See https://stackoverflow.com/a/73519818
class LegacyHttpAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, **kwargs):
        self.ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        self.ssl_context.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = urllib3.poolmanager.PoolManager(
            num_pools=connections, maxsize=maxsize,
            block=block, ssl_context=self.ssl_context)


legacy_session = requests.session()
legacy_session.mount("https://", LegacyHttpAdapter())


# towns.json and regions.json are derived from data by ČÚZK:
# https://geoportal.cuzk.cz/Default.aspx?mode=TextMeta&side=dSady_RUIAN_vse&metadataID=CZ-00025712-CUZK_SERIES-MD_RUIAN-STATY-SHP&head_tab=sekce-02-gp&menu=3327
def load_towns():
    towns_file = pathlib.Path(__file__).parent / "towns.json.gz"
    towns = {}
    with gzip.open(towns_file) as f:
        for feat in json.load(f)["features"]:
            name = feat["properties"]["name"]
            shape = shapely.geometry.shape(feat["geometry"])
            towns[name] = shape
    return towns
towns = load_towns()


def load_regions():
    regions_file = pathlib.Path(__file__).parent / "regions.json.gz"
    regions = []
    region_codes = []
    with gzip.open(regions_file) as f:
        for feat in json.load(f)["features"]:
            code = feat["properties"]["code"]
            region_codes.append(code)
            shape = shapely.geometry.shape(feat["geometry"])
            regions.append(shape)
    return region_codes, shapely.STRtree(regions)
region_codes, regions = load_regions()


def add_missing_town(stops):
    for stop in stops:
        yield stop
        point = shapely.Point(stop.lon, stop.lat)
        for town_name, town in towns.items():
            if point.within(town) and f"{town_name}," not in stop.name:
                yield Stop(f"{town_name}," + stop.name, stop.lat, stop.lon)


def add_missing_regions(stops):
    for stop in stops:
        if stop.region:
            yield stop
            continue
        point = shapely.Point(stop.lon, stop.lat)
        regions_idx = regions.query(point, predicate="within").tolist()
        if len(regions_idx) == 1:
            yield Stop(stop.name, stop.lat, stop.lon,
                       region_codes[regions_idx[0]])
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
        resp = requests.post(f"{url}/{layer}/query", data={
                "f": "json",
                "where": where,
                "outSR": "4326", # WGS84
                "outFields": ",".join(name_fields),
                "resultRecordCount": batch,
                "resultOffset": offset,
            })
        try:
            stops = resp.json()["features"]
        except Exception:
            print("Failed decoding response:", resp.text)
            return

        for stop in stops:
            attrs = stop["attributes"]
            name = ",".join((attrs[nf] or "").strip() for nf in name_fields)
            geom = stop["geometry"]
            yield Stop(name, geom["y"], geom["x"])

        print("Got", len(stops), "stops", file=sys.stderr)
        if len(stops) != batch: break
        offset += batch


def mapaduk_download_stops():
    URL = "https://provoz.kr-ustecky.cz/TMD/API/Map/GetStopMarkers"

    resp = requests.post(URL, json={})
    stops = resp.json()["ItemL"]

    for stop in stops:
        # These look like train stops
        if stop["PostNote"] == "žst.": continue
        yield Stop(stop["Name"], stop["Lat"], stop["Lng"])


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

    t = int(time.time() * 1000)
    date = dt.date.today().strftime("%d.%m.%Y")
    resp = requests.post(f"{URL}/AXSM/GetViewportObjects?rnd={t}",
        json = {
            "sid": "",
            "s": BBOX[0],
            "w": BBOX[1],
            "n": BBOX[2],
            "e": BBOX[3],
            "mppx": 14,
            "mapQuery": f"{instance},{date} *,all",
            "sOpt": "/z",
        })
    for stop in resp.json()["S"]:
        # Filter out train stops
        if stop["t"] == "T": continue
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

    transformer = pyproj.Transformer.from_crs(5514, 4326) # Křovák -> WGS 84

    with zip.open(shp_name) as shp_file, \
         zip.open(dbf_name) as dbf_file, \
         shapefile.Reader(shp=shp_file, dbf=dbf_file, encoding="UTF-8") as shp:
        stops = []
        for shrec in shp.shapeRecords():
            lat, lon = transformer.transform(*shrec.shape.points[0])
            name = shrec.record["POPIS_LONG"]
            stops.append(Stop(name, lat, lon))
        stops_agg = []
        # Collapse stops with the same names
        for stop_grp in itertools.groupby(stops, lambda s: s.name):
            stops = list(stop_grp[1])
            lat = statistics.mean(s.lat for s in stops)
            lon = statistics.mean(s.lon for s in stops)
            stops_agg.append(Stop(stop_grp[0], lat, lon))
        return stops_agg


def liberecky_kraj_download_stops():
    URL = "https://dopravnimapy.kraj-lbc.cz/opendata/zastavky_shp_wgs84.zip"
    zip_resp = legacy_session.get(URL)
    zip_io = io.BytesIO(zip_resp.content)
    zip = zipfile.ZipFile(zip_io)

    shp_name = next(n for n in zip.namelist() if n.endswith(".shp"))
    dbf_name = next(n for n in zip.namelist() if n.endswith(".dbf"))

    with zip.open(shp_name) as shp_file, \
         zip.open(dbf_name) as dbf_file, \
         shapefile.Reader(shp=shp_file, dbf=dbf_file, encoding="cp1250") as shp:
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
                yield Stop(group["municipality"] + "," + group["name"],
                           lat, lon, group["districtCode"])

def karlovarsky_kraj_download_stops():
    kv_stops = arcgis_download_stops(
        "https://geoportal.kr-karlovarsky.cz/arcgis/rest/services/UAP/UAP_Kompletni_obsah/MapServer",
        [296, 297, 298],
        ["PrvekNaz"])
    kv_stops_nonum = []
    for stop in kv_stops:
        name = re.sub(r'^("?)[0-9]* *', r'\1', stop.name)
        name = re.sub(r" *\([0-9]+\)$", "", name)
        if name == "": continue
        kv_stops_nonum.append(Stop(name, stop.lat, stop.lon))
    return kv_stops_nonum


def mapa_idsjmk_download_stops():
    def inner():
        stops_json = requests.get("https://mapa.idsjmk.cz/api/stops").json()
        for stop in stops_json["Stops"]:
            yield Stop(stop["Name"], stop["Latitude"], stop["Longitude"])

    return add_missing_town(inner())


def idsjmk_download_stops():
    stops = arcgis_download_stops( # URL is backing service for https://data.brno.cz/datasets/747a824783044377b6d07a8060e7769d_0/explore
        "https://services6.arcgis.com/fUWVlHWZNxUvTUh8/ArcGIS/rest/services/stops/FeatureServer",
        0,
        ["stop_name"])
    return add_missing_town(stops)


def most_download_stops():
    stops = arcgis_download_stops(
        # URL is backing service for https://opendata.mesto-most.cz/datasets/mestomost::zast%C3%A1vky-mhd/explore
        "https://mapy.mesto-most.cz/server/rest/services/Opendata/Zastavky_MHD_opendata/FeatureServer",
        0,
        ["NAZEV"])
    return add_missing_town(stops)


def ostrava_download_stops():
    return add_missing_town(
        zipped_geojson_download_stops(
            "https://mapy.ostrava.cz/opendata/data/opendata/zastavky_MHD_WGS84_gjson.zip",
            "zast_jm"))


def plzen_download_stops():
    return add_missing_town(
        zipped_geojson_download_stops(
            "https://opendata.plzen.eu/public/opendata/detail/9?detail-fileId=18&do=detail-downloadFile",
            "NAZEV",
            # The website requires some cookies for downloading?
            pre_urls=["https://opendata.plzen.eu/public/opendata/detail/9"]))


def write_stops_csv(outfile, stops):
    stops = add_missing_regions(stops)
    w = csv.writer(outfile.open("w"))
    for stop in stops:
        if stop.lat == 0 and stop.lon == 0: continue
        w.writerow([stop.name, stop.lat, stop.lon, stop.region or ""])


def download_all(outdir):
    (outdir / "other").mkdir(exist_ok=True)

    print("-- Downloading other/KarlovarskyKraj.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "KarlovarskyKraj.csv",
        karlovarsky_kraj_download_stops())

    print("-- Downloading other/KrajVysocina.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "KrajVysocina.csv",
        arcgis_download_stops(
            "https://mapy.kr-vysocina.cz/arcgis/rest/services/Doprava/SchemaLinek/MapServer",
            7,
            ["OBEC", "OBEC_CAST", "BLIZSI_MIS"]))

    print("-- Downloading other/MoravskoslezskyKraj.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MoravskoslezskyKraj.csv",
        arcgis_download_stops(
            "https://gis.msk.cz/arcgis/rest/services/public/dsh_bus/MapServer",
            0,
            ["OBEC", "OBEC_CAST", "BLIZSI_MIS"]))

    print("-- Downloading other/UsteckyKraj.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "UsteckyKraj.csv",
        arcgis_download_stops(
            "https://ags.kr-ustecky.cz/arcgis/rest/services/Doprava/zastavky/MapServer",
            0,
            ["NAZEV"]))

    print("Downloading other/Mapa_IDSJMK.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "Mapa_IDSJMK.csv",
        mapa_idsjmk_download_stops())

    print("Downloading other/IDSJMK.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "IDSJMK.csv",
        idsjmk_download_stops())

    print("-- Downloading other/PlzenskyKraj.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "PlzenskyKraj.csv",
        arcgis_download_stops(
            "https://mapy.plzensky-kraj.cz/ArcGIS/rest/services/zastavky/MapServer",
            1,
            ["OZNACENI"]))

    print("Downloading other/MapaDUK.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MapaDUK.csv",
        mapaduk_download_stops())

    print("Downloading other/MapaIREDO.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MapaIREDO.csv",
        tmapy_download_stops("https://tabule.oredo.cz"))

    print("Downloading other/MapaIDSOK.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MapaIDSOK.csv",
        tmapy_download_stops("https://cestujok.cz"))

    print("Downloading other/MPVNet_PID.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MPVNet_PID.csv",
        mpvnet_download_stops("PID"))

    print("Downloading other/MPVNet_ODIS.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MPVNet_ODIS.csv",
        mpvnet_download_stops("ODIS"))

    print("Downloading other/MPVNet_Zlin.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MPVNet_Zlin.csv",
        mpvnet_download_stops("ZLIN"))

    print("Downloading other/MPVNet_IDOL.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MPVNet_IDOL.csv",
        mpvnet_download_stops("IDOL"))

    print("Downloading other/JihoceskyKraj.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "JihoceskyKraj.csv",
        jihocesky_kraj_download_stops())

    print("Downloading other/LibereckyKraj.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "LibereckyKraj.csv",
        liberecky_kraj_download_stops())

    print("Downloading other/PID.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "PID.csv",
        pid_download_stops())

    print("Downloading other/Most.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "Most.csv",
        most_download_stops())

    print("Downloading other/Ostrava.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "Ostrava.csv",
        ostrava_download_stops())

    print("Downloading other/Plzen.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "Plzen.csv",
        plzen_download_stops())


if __name__ == "__main__":
    outdir = pathlib.Path(sys.argv[1])
    download_all(outdir)
