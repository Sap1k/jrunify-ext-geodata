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
| Ústecký kraj / DÚK | integrated | ArcGIS and QRide catalogues; the live map duplicated QRide positions and was retired when its certificate expired |
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
searches. All accepted residual coordinates from targeted OSM, Mapy, and
refreshed regional catalogues are consolidated in `other/gapfill.csv`; the file
contained 336 stop-level name/region/country identities before the 2026-10-02 pass. Reconciliation of the
original regional-adjacent work list left zero unresolved identities. Mapy raw
responses and the credential were not retained. Approximate town-coordinate
rows were removed when JrUtil adopted route-derived estimates for unresolved stops.

## 2026-10-02 refresh

All sources were attempted. The Moravskoslezský kraj layer moved to a new
public multipoint FeatureServer (the old service now requires a token), and
Žďár's kdyPrijede endpoint now rejects the default python-requests User-Agent;
both were fixed. MapaDUK was retired after verifying that QRide covers every
referenced name once `Ústí n.L.` is expanded (one renamed stop went to
`Retained.csv`). CestujOK (IDSOK) and DPMLJ reset the TLS handshake from the
non-Czech refresh host and the Plzeň portal answered 503; their previous
snapshots are kept. The MPVNet catalogues shrank by 10-40 % upstream (tiling the
query returns even fewer stops), so names that disappeared but are still
referenced by the feed were preserved in `Retained.csv` (319 names).

### Residual gap-fill

The work list was the 155 distinct `[?]` names of the 2026-10-02 bundle, with
okres codes taken from the CIS JŘ JDF exports (portal.cisjr.cz) and route
anchors from the bundle's stop times. After the refresh, 65 names have an
exact name/okres catalogue row. 38 more were added to `gapfill.csv`, which now
holds 431 rows: 14 local catalogue spelling variants, 10 targeted Nominatim
bus stops and 14 manually reviewed catalogue variants. Two existing rows were
corrected: `Nemilkov,rozc.1.0` carried Klatovy coordinates under okres MO, and
`Těšovice,obecní úřad` was labelled PT instead of SO. The Nominatim stop
classifier no longer treats roads and squares (`highway=residential`,
`pedestrian` and similar) as stops; candidates of that kind were rejected.

Thirteen names already had a plausible row before the run (for example
Nasavrky, Plazy and Mastník), so JrUtil rejected those matches for other
reasons, most likely travel-time conflicts with neighbouring matches.
The 38 that still have no row are mostly special or tourist services with
unusable route estimates (Prague centre, Vranov dam, Brno circuit), factory
gates and stops with no public catalogue or OSM stop object.
