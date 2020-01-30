#!/usr/bin/env python3
import sys
import csv
import requests

# I don't know who made this app, sorry for the naming

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    resp = requests.get(url + "/idspublicservices/api/station")
    stops = resp.json()

    w = csv.writer(sys.stdout)
    for stop in stops:
        if "lat" not in stop:
            print("Skipping stop:", stop, file=sys.stderr)
            continue
        w.writerow([stop["name"], stop["lat"], stop["lon"]])
