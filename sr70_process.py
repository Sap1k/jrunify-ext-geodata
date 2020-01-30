#!/usr/bin/env python3
import sys
import csv
import re

def dms_to_decimal(dms):
    match = re.match("[NE](.*)°(.*)'(.*)\"", dms)
    if match.group(1).strip() == "": return 0
    d = int(match.group(1))
    m = int(match.group(2) if match.group(2).strip() != "" else 0)
    s = float(match.group(3).replace(",", ".").replace(" ", ""))
    return d + m/60 + s/3600

reader = csv.reader(sys.stdin)
next(reader) # Skip header
for row in reader:
    sr70 = row[0]
    name = row[1]
    lon = dms_to_decimal(row[21])
    lat = dms_to_decimal(row[22])
    print(f'{sr70},"{name}",{lat},{lon}')
