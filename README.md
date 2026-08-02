This data is used by [JrUnify-cloud](https://gitlab.com/dvdkon/jrunify-cloud)

CSV rows use the stop-only contract
`name,latitude,longitude,okres,country`. Approximate town coordinates are not
valid geodata. External ISO country codes are normalized to the historical JDF
identifiers by `download.py`.

Run a complete refresh into staging before replacing checked-in files:

```sh
python download.py /path/to/empty-staging-directory
```

The updater validates rows, removes spatially coincident duplicates, stages
each source atomically, and reports every failed source after attempting the
full inventory. See [SOURCE_AUDIT.md](SOURCE_AUDIT.md) for the DADOF audit and
known retired endpoints. Pass the repository's `other` directory to JrUtil;
the `rail` directory uses the separate SR70 four-column contract.

For residual coordinates, `gapfill.py osm` accepts one or more Overpass
bounding boxes and caches the batched extracts. OSM candidates without
municipality tags may match through the preloaded okres index. `gapfill.py mapy`
is the final fallback and reads its credential only from `MAPY_API_KEY`. Both commands accept an input CSV
with `stop_id,name,municipality,region,country`, write accepted stop coordinates
to the geodata CSV, and put all ambiguous/missing rows in a review CSV. Mapy raw
responses are never stored.

For a small residual, prefer `gapfill.py osm-search`: it performs one cached,
rate-limited Nominatim search per stop instead of serial Overpass boxes. Nearby
platforms with the same exact identity are emitted as one stop-place centroid;
distant or competing clusters remain in review. Mapy can additionally write a
structured `--candidate-review` CSV containing candidate labels and
coordinates, but no raw responses or credential. Regenerated stop IDs are not
used as matching configuration; exact locality suffixes and route context
handle incorrect source municipalities without run-specific overrides.

`gapfill.py audit GTFS_DIRECTORY MERGED_JDF_ZIP OUTPUT.csv` builds that input
from an existing bundle, so refreshing coordinates does not require another
JrUtil conversion. Its default is `--coordinate-status missing`. Use
`--coordinate-status estimated` to reconsider route-derived coordinates, or
`all` for both. These modes read `source_stop_metadata.parquet` (override with
`--metadata`) rather than relying on the rendered GTFS name suffix and therefore
require PyArrow. The audit embeds deduplicated preceding/following timed anchors
and ignores degenerate trips with fewer than two distinct stop places.
Use `--minimum-run-length 5 --include-unresolved-termini` to produce a focused
quality work list. Its additional columns identify the longest consecutive
unresolved run, terminal involvement, affected trips and routes. These fields
prioritize review; they do not weaken candidate validation.

For example, refine estimated coordinates without making Mapy a live build
dependency:

```sh
uv run --with pyarrow --with requests --with lxml --with pyproj \
  --with shapely --with pyshp python gapfill.py audit \
  BUNDLE/gtfs-intermediate MERGED_JDF.zip estimated.csv \
  --coordinate-status estimated
MAPY_API_KEY=... uv run --with requests --with lxml --with pyproj \
  --with shapely --with pyshp python gapfill.py mapy \
  estimated.csv accepted.csv --review review.csv \
  --candidate-review candidates.csv \
  --gtfs BUNDLE/gtfs-intermediate --jdf MERGED_JDF.zip
python gapfill.py merge other/gapfill.csv accepted.csv merged.csv
```

Mapy searches only POIs, hard-restricts the country, and prefers the current
estimate. Full names, expanded JDF abbreviations, and locality-qualified name
variants are tried. Foreign OSM/Nominatim searches additionally try localized
stop/station terminology and retain `name:de`, `name:pl` and `name:sk` aliases.
Exact and fuzzy matches (default threshold `0.70`, with a
`0.10` winner margin) may be accepted, but a candidate for an estimated stop
must satisfy the same 2 km plus 150 km/h scheduled-time ceiling as JrUtil.
When normal name selection remains ambiguous, an estimated stop may use one
clearly dominant route-supported candidate within 10 km; it requires at least
`0.55` name similarity and a `0.12` combined name/distance margin.
Exact locality suffixes may repair a bad JDF municipality only when that route
check succeeds. Town/address/unrelated POIs and distant same-name stops remain
in review. Accepted rows use the five-column stop contract; merge them into the
checked supplement so ordinary feed generation remains offline and deterministic.

After enabling JrUtil's `regional-adjacent` route policy, build the actionable
post-filter work list without another conversion or a JrUtil cache:

```sh
python residual_plan.py GTFS_DIRECTORY MERGED_JDF_ZIP AUDIT.csv WORKLIST.csv \
  --external-geodata other
```

The utility uses the emitted GTFS trip set for service validity, applies the
same international-route distance rules to the merged JDF, and removes only
exact refreshed-source matches with compatible geography and a candidate
cluster under one kilometre. The resulting CSV includes source stop IDs, route
distinctions, route names and the recommended next matching stage.

# Railway sources

## SR70.csv

Extract from [official
SR70](https://provoz.spravazeleznic.cz/Portal/ViewArticle.aspx?oid=34462) made with
`sr70_download.py`. The current checked snapshot was generated from
`Číselník SR70 od 15. srpna 2026.xlsx`, effective 2026-08-15, with source
SHA-256
`6adac3d1ebce48ee09f6cf75032d09c43ad9a957cf938c37a59872499971c6f0`.

Regenerate both rail snapshots atomically from the same downloaded workbook:

```sh
python sr70_download.py --output-dir rail "/path/to/Číselník SR70.xlsx"
```

## SR70\_Nazev20.csv

Variant of `SR70.csv` with names from column `NÁZEV20`. Intended for matching
GRAPP names and for compact passenger-facing CZPTT fallback route endpoints.
Official values are retained verbatim; presentation-specific cleanup such as
removing the standalone terminal ` z` or ` nz` happens in the consumer.

# Other sources

This includes anything other than railways. Buses, trams, funiculars...

TODO: Some of these sources include CIS JŘ IDs and distinguish stop posts. We
should make use of them, but that requires CIS JŘ IDs in JDF.

## LibereckyKraj.csv

Open data: https://dopravnimapy.kraj-lbc.cz/opendata/?id=584a7ad7-1680-4d8d-a20b-7068c371c416

## JihoceskyKraj.csv

Open data: https://geoportal.kraj-jihocesky.gov.cz/gs/zastavky-verejne-dopravy/

## PID.csv

Open data: http://opendata.praha.eu/dataset/zastavky-pid-jednotlive-oznacniky-geodata/resource/8c912738-d4cb-41ca-b223-a8e455cd4c80

## MPVNet\_PID.csv

Scraped: https://mpvnet.cz/pid/map

## MPVNet\_ODIS.csv

Scraped: https://mpvnet.cz/odis/map

## MPVNet\_Zlin.csv

Scraped: https://mpvnet.cz/zlin/map

## MPVNet\_IDOL.csv

Scraped: https://mpvnet.cz/idol/map

## IDSJMK\_Map.csv

Scraped: https://mapa.idsjmk.cz/

## MapaDUK.csv

Scraped: https://provoz.dopravauk.cz/sprinter

## MapaIREDO.csv

Scraped: https://tabule.oredo.cz/idspublic/

## MapaIDSOK.csv

Scraped: https://cestujok.cz/idspublic/

## UsteckyKraj.csv

From ArcGIS: https://ags.kr-ustecky.cz/arcgis/rest/services/Doprava/zastavky/MapServer

## PlzenskyKraj.csv

From ArcGIS: http://mapy.plzensky-kraj.cz/ArcGIS/rest/services/zastavky/MapServer/1

## MoravskoslezskyKraj.csv

From ArcGIS: https://gis.msk.cz/arcgis/rest/services/public/dsh\_bus/MapServer/6

## KrajVysocina.csv

From ArcGIS: http://geoportal.kr-vysocina.cz/arcgis/rest/services/Trasy\_dopravy/zastavky/MapServer

## KarlovarskyKraj.csv

From ArcGIS: http://geoportal.kr-karlovarsky.cz/arcgis/rest/services/UAP/UAP\_msd/MapServer
