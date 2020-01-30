#!/usr/bin/env python3
import sys
import requests
import csv

URL = "https://mapa.idsjmk.cz"

resp = requests.get(URL + "/api/stops.json")
stops = resp.json()
w = csv.writer(sys.stdout)
for stop in stops:
    # There are some duplicates, weird...
    if stop["Latitude"] == 0: continue
    w.writerow([stop["Name"], stop["Latitude"], stop["Longitude"]])
