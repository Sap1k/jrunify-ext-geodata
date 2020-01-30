#!/usr/bin/env python3
import sys
import csv
import time
import requests
import datetime as dt

URL = "https://mpvnet.cz"
# Whole Czech Rep.
BBOX = [48.195, 12.000, 51.385, 18.951]

def download_stops(instance):
    t = int(time.time() * 1000)
    date = dt.date.today().strftime("%d.%m.%Y")
    resp = requests.post(f"{URL}/AXSM/GetViewportObjects?rnd={t}",
        json={
            "sid": "",
            "s": BBOX[0],
            "w": BBOX[1],
            "n": BBOX[2],
            "e": BBOX[3],
            "mppx": 14,
            "mapQuery": f"{instance},{date} *,all",
            "sOpt": "/z",
        })
    return resp.json()["S"]

def stops_to_csv(stops):
    w = csv.writer(sys.stdout)
    for stop in stops:
        w.writerow([stop["n"], stop["x"], stop["y"]])

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <instance>", file=sys.stderr)
        sys.exit(1)

    stops = download_stops(sys.argv[1])
    stops_to_csv(stops)
