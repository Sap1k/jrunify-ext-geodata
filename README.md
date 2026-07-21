This data is used by [JrUnify-cloud](https://gitlab.com/dvdkon/jrunify-cloud)

CSV rows use `name,latitude,longitude,okres,country` and may include a sixth
precision field: `S` for a stop-level coordinate or `T` for an explicitly
approximate town coordinate. Five-column files mean `S`. External ISO country
codes are normalized to the historical JDF identifiers by `download.py`.

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
bounding boxes and caches the batched extracts. Supply a separate
`--nominatim-cache`; it is consulted only for unique unresolved foreign
municipalities, sequentially at no more than one request per second, and emits
town-precision (`T`) rows. OSM candidates without municipality tags may match
through the preloaded okres index. `gapfill.py mapy` is the final fallback and
reads its credential only from `MAPY_API_KEY`. Both commands accept an input CSV
with `stop_id,name,municipality,region,country`, write only unique exact
normalized matches to the geodata CSV, and put all ambiguous/missing rows in a
review CSV. Mapy raw responses and rejected candidates are never stored.

For a small residual, prefer `gapfill.py osm-search`: it performs one cached,
rate-limited Nominatim search per stop instead of serial Overpass boxes. Nearby
platforms with the same exact identity are emitted as one stop-place centroid;
distant or competing clusters remain in review. `--context-overrides` applies
the checked `gapfill-context-overrides.csv` parent-municipality corrections to
either `osm-search` or `mapy`. Mapy can additionally write a structured
`--candidate-review` CSV containing candidate labels and coordinates, but no
raw responses or credential.

`gapfill.py audit GTFS_DIRECTORY MERGED_JDF_ZIP OUTPUT.csv` builds that input
from an existing bundle, so refreshing coordinates does not require another
JrUtil conversion. Run Mapy only on the review/residual left by the open-data
and OSM stages.

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
`sr70_process.py`.

## SR70\_Nazev20.csv

Variant of `SR70.csv` with names from column `NÁZEV20`. Intended for matching
GRAPP names.

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
