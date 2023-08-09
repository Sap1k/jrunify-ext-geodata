This data is used by [JrUnify-cloud](https://gitlab.com/dvdkon/jrunify-cloud)

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
