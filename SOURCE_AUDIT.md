# Stop-coordinate source audit

Audit performed 2026-07-20 against the DADOF `PZ` entries and `RTP` entries
that expose a stable stop catalogue. Publicly reachable catalogues are eligible;
the updater does not impose a licence gate.

| DADOF source | Classification | Implementation/status |
| --- | --- | --- |
| PID open data | integrated | `PID.csv` and `MPVNet_PID.csv` |
| DPMLJ GTFS | newly usable | official `https://www.dpmlj.cz/gtfs.zip`, now `DPMLJ.csv`; tariff zones are deterministically expanded to Liberec/Jablonec JDF name components |
| Brno / IDS JMK | integrated | regional ArcGIS and live-map catalogues |
| Jihočeský kraj | integrated | regional GeoJSON catalogue |
| Liberecký kraj / IDOL map | integrated/duplicate | regional catalogue plus MPVNet; the second DADOF map does not add a distinct stable catalogue |
| Královéhradecký kraj | newly usable | official ArcGIS bus-stop layer, now `KralovehradeckyKraj.csv`; railway stations duplicate national SR70 |
| Plzeňský kraj and Plzeň | integrated | regional ArcGIS and municipal catalogue |
| Karlovarský kraj | obsolete | listed ArcGIS service now reports that it is stopped; retain the last checked-in snapshot pending a replacement endpoint |
| Olomoucký kraj | duplicate | IDSOK already covers the regional catalogue; DPMO adds a directly maintained urban catalogue |
| DPMO GTFS | newly usable | official `https://www.dpmo.cz/doc/dpmo-olomouc-cz.zip`, now `DPMO.csv`; bare urban names are explicitly prefixed with Olomouc |
| Ústecký kraj / DÚK | integrated | ArcGIS, QRide and live-map catalogues |
| MPVNet regional maps | integrated | PID, ODIS, IDOL, Zlín and JIKORD catalogues |
| IDS JMK live maps | integrated | stable stop endpoint; retired BMHD iRIS is obsolete |
| old IREDO map | obsolete | first-generation endpoint no longer returns JSON and was announced for retirement |
| new IREDO map | integrated | `MapaIREDO2.csv` |
| CestujOK / IDSOK | integrated | `MapaIDSOK.csv` |
| JIKORD | integrated | direct station endpoint plus MPVNet catalogue |
| DPO / ODIS | integrated | municipal and MPVNet catalogues |
| DSZO | integrated/duplicate | MPVNet Zlín is the stable stop catalogue; vehicle-only endpoints add no stop coverage |
| Most | integrated | public ArcGIS stop layer |
| Žďár nad Sázavou | integrated | kdyPrijede catalogue |
| Vysočina | integrated | ABIRUN VDV plus regional ArcGIS catalogue |
| Středočeský GIS | obsolete | DADOF ArcGIS URL returns 404; its deliberately low-precision stops are also covered by current PID data |
| CRWS | access-controlled | useful stop search, but not a public bulk catalogue |
| realtime vehicle-only sources | technically unusable | GRAPP, KORDIS, vehicle trackers and similar RTP feeds do not expose a stable stop catalogue beyond an integrated source |

The 2026-07-20 clean refresh successfully produced 24 catalogues. Two legacy
catalogues failed as described above. New-source row counts after within-source
deduplication were KHK 4,454, DPMLJ 581 and DPMO 373. All promoted files are UTF-8
CSV with finite in-range coordinates; source-country identifiers are converted
to historical JDF codes while writing.

The refresh is intentionally separate from JrUtil. A targeted route-batch check
after the refresh matched all 20 stops in an Olomouc fixture without a JrUtil
cache; 40 candidates took the strict okres path after treating the observed JDF
alias `OL` as boundary-data code `OC`, and DPMO participated in five matches.

The broad serial Overpass strategy proved unsuitable for the small residual:
one endpoint timed out and a second run spent minutes walking municipality
boxes. It was replaced by cached, one-request-per-second targeted Nominatim
searches. The completed regional-adjacent work list contains 116 identities:
14 are in `osm-gapfill.csv`, 100 in `mapy-gapfill.csv`, and 2 exact rows recovered
from refreshed regional catalogues are in `source-recovered-gapfill.csv`.
Reconciliation leaves zero unresolved work-list identities. Mapy raw responses
and the credential were not retained; the one POI-only manual acceptance
(`Březina,škola`) is explicitly town precision.
