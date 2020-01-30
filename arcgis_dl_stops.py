#!/usr/bin/env python3
import sys
import csv
import requests
from docopt import docopt

def download_stops(url, layer, name_fields, paginate):
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
        all_stops += stops
        print("Got", len(stops), "stops", file=sys.stderr)
        if len(stops) < batch or not paginate: break
        offset += batch

    return all_stops

def stops_to_csv(stops, name_fields):
    w = csv.writer(sys.stdout)
    for stop in stops:
        attrs = stop["attributes"]
        name = ",".join(attrs[nf] or "" for nf in name_fields)
        geom = stop["geometry"]
        w.writerow([name, geom["y"], geom["x"]])

if __name__ == "__main__":
    args = docopt("""
Usage: arcgis_dl_stops.py [--paginate] <url> <layer-id> <name-fields>
    """)

    name_fields = args["<name-fields>"].split(",")

    stops = download_stops(
        args["<url>"], args["<layer-id>"], name_fields,
        args["--paginate"])
    stops_to_csv(stops, name_fields)
