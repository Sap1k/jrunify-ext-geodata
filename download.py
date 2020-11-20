#!/usr/bin/env python3
import io
import re
import sys
import csv
import json
import time
import pathlib
import zipfile
import tempfile
import itertools
import statistics
import datetime as dt
from collections import namedtuple
import pyproj
import requests
import shapefile

Stop = namedtuple("Stop", ["name", "lat", "lon"])

def arcgis_download_stops(url, layer, name_fields, paginate=True):
    batch = 1000
    offset = 0
    all_stops = []
    while True:
        print("Downloading with offset", offset, file=sys.stderr)
        resp = requests.post(f"{url}/{layer}/query", {
                "f": "json",
                "where": "1=1",
                "outSR": "4326", # WGS84
                "outFields": ",".join(name_fields),
                "resultRecordCount": batch,
                "resultOffset": offset,
            })
        stops = resp.json()["features"]

        for stop in stops:
            attrs = stop["attributes"]
            name = ",".join(attrs[nf] or "" for nf in name_fields)
            geom = stop["geometry"]
            yield Stop(name, geom["y"], geom["x"])

        print("Got", len(stops), "stops", file=sys.stderr)
        if len(stops) < batch or not paginate: break
        offset += batch

def mapaduk_download_stops():
    URL = "https://provoz.dopravauk.cz/sprinter/Pages/Others/map.aspx"

    resp = requests.post(URL + "/GetStopsData", json={})
    stops = json.loads(resp.json()["d"])

    w = csv.writer(sys.stdout)
    for stop in stops:
        yield Stop(stop["Name"], stop["Lat"], stop["Lng"])

def tmapy_download_stops(url):
    # I don't know who made this app, sorry for the naming

    resp = requests.get(url + "/idspublicservices/api/station")
    stops = resp.json()

    for stop in stops:
        if "lat" not in stop:
            continue
        yield Stop(stop["name"], stop["lat"], stop["lon"])

def mapa_idsjmk_download_stops():
    URL = "https://mapa.idsjmk.cz"

    resp = requests.get(URL + "/api/stops.json")
    stops = resp.json()
    for stop in stops:
        # There are some duplicates, weird...
        if stop["Latitude"] == 0: continue
        yield Stop(stop["Name"], stop["Latitude"], stop["Longitude"])

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
        yield Stop(stop["n"], stop["x"], stop["y"])

def jihocesky_kraj_download_stops():
    # TODO: Get the current URL automatically, or ask them to create a
    # permanent one
    URL = "https://geoportal.kraj-jihocesky.gov.cz/gs/data/uploads/opendata/zastavky_jck_20200609_shp.zip"
    zip_resp = requests.get(URL)
    zip_io = io.BytesIO(zip_resp.content)
    zip = zipfile.ZipFile(zip_io)

    shp_name = next(n for n in zip.namelist() if n.endswith(".shp"))
    dbf_name = next(n for n in zip.namelist() if n.endswith(".dbf"))

    transformer = pyproj.Transformer.from_crs(5514, 4326) # Křovák -> WGS 84

    with zip.open(shp_name) as shp_file, \
         zip.open(dbf_name) as dbf_file, \
         shapefile.Reader(shp=shp_file, dbf=dbf_file, encoding="852") as shp:
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
    zip_resp = requests.get(URL)
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
    URL = "http://opendata.iprpraha.cz/CUR/DOP/DOP_PID_ZASTAVKY_TS_B/WGS_84/DOP_PID_ZASTAVKY_TS_B.json"
    resp = requests.get(URL)
    for stop in resp.json()["features"]:
        lon, lat = stop["geometry"]["coordinates"]
        yield Stop(stop["properties"]["ZAST_NAZEV"], lat, lon)

def write_stops_csv(outfile, stops):
    w = csv.writer(outfile.open("w"))
    for stop in stops:
        w.writerow([stop.name, stop.lat, stop.lon])

def download_all(outdir):
    (outdir / "other").mkdir(exist_ok=True)

    print("-- Downloading other/KarlovarskyKraj.csv", file=sys.stderr)
    kv_stops = arcgis_download_stops(
        "http://geoportal.kr-karlovarsky.cz/arcgis/rest/services/UAP/UAP_msd/MapServer",
        290,
        ["PrvekNaz"])
    kv_stops_nonum = []
    for stop in kv_stops:
        name = re.sub(r'^("?)[0-9]* *', r'\1', stop.name)
        kv_stops_nonum.append(Stop(name, stop.lat, stop.lon))
    write_stops_csv(outdir / "other" / "KarlovarskyKraj.csv", kv_stops_nonum)

    print("-- Downloading other/KrajVysocina.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "KrajVysocina.csv",
        arcgis_download_stops(
            "http://geoportal.kr-vysocina.cz/arcgis/rest/services/Trasy_dopravy/zastavky/MapServer",
            0,
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

    # Seems broken right now, sends "Unable to complete Query operation."
    #print("-- Downloading other/PlzenskyKraj.csv", file=sys.stderr)
    #write_stops_csv(outdir / "other" / "PlzenskyKraj.csv",
    #    arcgis_download_stops(
    #        "http://mapy.plzensky-kraj.cz/ArcGIS/rest/services/zastavky/MapServer",
    #        1,
    #        ["OZNACENI"]))

    print("Downloading other/MapaDUK.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MapaDUK.csv",
        mapaduk_download_stops())

    print("Downloading other/MapaIREDO.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MapaIREDO.csv",
        tmapy_download_stops("https://tabule.oredo.cz"))

    print("Downloading other/MapaIDSOK.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "MapaIDSOK.csv",
        tmapy_download_stops("https://cestujok.cz"))

    print("Downloading other/IDSJMK_Map.csv", file=sys.stderr)
    write_stops_csv(outdir / "other" / "IDSJMK_Map.csv",
        mapa_idsjmk_download_stops())

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

if __name__ == "__main__":
    outdir = pathlib.Path(sys.argv[1])
    download_all(outdir)
