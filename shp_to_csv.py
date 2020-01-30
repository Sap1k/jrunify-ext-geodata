#!/usr/bin/env python3
import sys
import csv
import pyproj
import shapefile
from docopt import docopt

PROJ_S_JTSK = pyproj.CRS.from_epsg(5514)
PROJ_WGS_84 = pyproj.CRS.from_epsg(4326)

def process_shp(filename, encoding, name_field, from_proj):
    trans = pyproj.Transformer.from_proj(from_proj, PROJ_WGS_84)
    writer = csv.writer(sys.stdout)
    with shapefile.Reader(filename, encoding=encoding) as sf:
        assert(sf.shapeType == shapefile.POINT)
        for point in sf.shapeRecords():
            x, y = point.shape.points[0]
            name = point.record[name_field]
            lat, lon = trans.transform(x, y)
            writer.writerow((name, lat, lon))

if __name__ == "__main__":
    args = docopt("""
Usage: shp_to_csv.py <in-path> <name-field> [options]

Options:
  --enc=ENC          Encoding [default: UTF8]
  --from-proj=PROJ   Source projection [default: S-JTSK]
    """)

    from_proj = PROJ_S_JTSK \
                if args["--from-proj"] == "S-JTSK" \
                else pyproj.CRS.from_epsg(args["--from-proj"])
    process_shp(
        args["<in-path>"], args["--enc"], args["<name-field>"], from_proj)
