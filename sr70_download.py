#!/usr/bin/env python3
import datetime as dt
import io
import lxml.html
import openpyxl
import re
import requests
import sys

def get_latest_url():
    resp = requests.get("https://provoz.spravazeleznic.cz/Portal/ViewArticle.aspx?oid=34462")
    doc = lxml.html.fromstring(resp.content)
    xls_rows = doc.xpath("//div[@class='innerContent']//tr[.//img[contains(@src,'XLS')]]")
    candidates = []
    for row in xls_rows:
        candidates.append((
            dt.datetime.strptime(row.xpath("td[5]/text()")[0],
                                 "%d.%m.%Y %H:%M:%S"),
            row.xpath("td[1]/a/@href")[0],
        ))
    candidates.sort()
    return "https://provoz.spravazeleznic.cz/Portal/" + candidates[-1][1]

def dms_to_decimal(dms):
    match = re.match("[NE](.*)°(.*)'(.*)\"", dms)
    assert match
    if match.group(1).strip() == "": return 0
    d = int(match.group(1))
    m = int(match.group(2) if match.group(2).strip() != "" else 0)
    s = float(match.group(3).replace(",", ".").replace(" ", ""))
    return d + m/60 + s/3600

def process(file, name_col, just_names, only_stops):
    wb = openpyxl.load_workbook(file)
    sh = wb.active
    header = [c.value for c in sh[1]]
    for rownum in range(2, sh.max_row + 1):
        row = [c.value for c in sh[rownum]]
        if only_stops:
            kind_num = row[header.index("Kvalifikátor")]
            # See official documentation for meaning of numbers
            if kind_num not in [1, 61, 23, 24, 27, 28, 33, 34, 37, 38,
                                43, 44, 52, 62]:
                continue
        sr70 = int(row[header.index("SR70")])
        name = row[header.index(name_col)]
        if just_names:
            print(f'{sr70},"{name}"')
        else:
            lon = dms_to_decimal(row[header.index("GPS X")])
            lat = dms_to_decimal(row[header.index("GPS Y")])
            print(f'{sr70},"{name}",{lat},{lon}')

def main():
    name_col = "Tarifní název"
    if sys.argv[1].startswith("--name="):
        name_col = sys.argv[1][len("--name="):]
        del sys.argv[1]
    just_names = False
    if sys.argv[1] == "--just-names":
        just_names = True
        del sys.argv[1]
    only_stops = False
    if sys.argv[1] == "--only-stops":
        only_stops = True
        del sys.argv[1]
    if len(sys.argv) == 1:
        resp = requests.get(get_latest_url())
        infile = io.BytesIO(resp.content)
    elif len(sys.argv) == 2:
        infile = open(sys.argv[1])
    else:
        print("Usage: [--name=NAME] [--just-names] [--only-stops] [<input xls>]",
              file=sys.stderr)
        sys.exit(1)

    process(infile, name_col, just_names, only_stops)

if __name__ == "__main__":
    main()
