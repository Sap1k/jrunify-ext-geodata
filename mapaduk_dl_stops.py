#!/usr/bin/env python3
import sys
import csv
import json
import requests

URL = "https://provoz.dopravauk.cz/sprinter/Pages/Others/map.aspx"

resp = requests.post(URL + "/GetStopsData", json={})
stops = json.loads(resp.json()["d"])

w = csv.writer(sys.stdout)
for stop in stops:
    w.writerow([stop["Name"], stop["Lat"], stop["Lng"]])
