#!/usr/bin/env python3
import sys
import re
import xlrd

def dms_to_decimal(dms):
    match = re.match("[NE](.*)°(.*)'(.*)\"", dms)
    if match.group(1).strip() == "": return 0
    d = int(match.group(1))
    m = int(match.group(2) if match.group(2).strip() != "" else 0)
    s = float(match.group(3).replace(",", ".").replace(" ", ""))
    return d + m/60 + s/3600

name_col = "Tarifní název"
if len(sys.argv) > 2 and sys.argv[1].startswith("--name="):
    name_col = sys.argv[1][len("--name="):]
    del sys.argv[1]
just_names = False
if len(sys.argv) > 2 and sys.argv[1] == "--just-names":
    just_names = True
    del sys.argv[1]
only_stops = False
if len(sys.argv) > 2 and sys.argv[1] == "--only-stops":
    only_stops = True
    del sys.argv[1]
if len(sys.argv) != 2:
    print("Usage: [--name=NAME] [--just-names] [--only-stops] <input xls>",
          file=sys.stderr)
    sys.exit(1)

wb = xlrd.open_workbook(sys.argv[1])
sh = wb.sheet_by_index(0)
header = sh.row_values(0)
for rownum in range(1, sh.nrows):
    row = sh.row_values(rownum)
    if only_stops:
        kind_num = row[header.index("Kvalifikátor")]
        if kind_num not in [1, 61]: continue
    sr70 = int(row[header.index("SR70")])
    name = row[header.index(name_col)]
    if just_names:
        print(f'{sr70},"{name}"')
    else:
        lon = dms_to_decimal(row[header.index("GPS X")])
        lat = dms_to_decimal(row[header.index("GPS Y")])
        print(f'{sr70},"{name}",{lat},{lon}')
