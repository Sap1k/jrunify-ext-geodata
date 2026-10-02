import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import gapfill


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise gapfill.requests.HTTPError("request failed")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


class GapfillTests(unittest.TestCase):
    def stop(self, name="Hradec Králové,OD TESCO a ATRIUM", country="CZ"):
        return gapfill.StopQuery("42", name, "Hradec Králové", "HK", country)

    def test_normalization_handles_case_punctuation_and_diacritics(self):
        self.assertEqual(
            gapfill.normalize_text("OD TESCO a ATRIUM"),
            gapfill.normalize_text("od Tesco & Atrium"),
        )

    def test_checked_gapfills_are_consolidated_and_have_stable_unique_keys(self):
        root = Path(__file__).parent
        self.assertEqual(
            ["gapfill.csv"],
            [path.name for path in (root / "other").glob("*gapfill*.csv")],
        )
        with (root / "other" / "gapfill.csv").open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            rows = [row for row in gapfill.csv.reader(stream) if row]
        self.assertTrue(rows)
        self.assertTrue(all(len(row) == 5 for row in rows))
        keys = [gapfill.geodata_row_key(row) for row in rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_nominatim_search_returns_transit_stop_candidate(self):
        session = FakeSession(
            [
                FakeResponse(
                    [
                        {
                            "lat": "49.8584449",
                            "lon": "12.3538413",
                            "category": "highway",
                            "type": "bus_stop",
                            "name": "Lodermühl",
                            "display_name": "Lodermühl, Tirschenreuth, Deutschland",
                            "address": {"town": "Tirschenreuth", "country_code": "de"},
                            "namedetails": None,
                        }
                    ]
                )
            ]
        )
        stop = gapfill.StopQuery("1", "Lodermühl", "Tirschenreuth", "", "D")
        with TemporaryDirectory() as directory:
            candidates = gapfill.NominatimClient(
                session=session, retries=1, delay=0
            ).search(stop, Path(directory))
        self.assertEqual("stop", candidates[0].kind)
        self.assertEqual("D", gapfill.normalize_country_code(candidates[0].country))
        self.assertEqual(
            candidates[0], gapfill.choose_exact_candidate(stop, candidates)
        )

    def test_nominatim_keeps_non_transit_result_as_poi(self):
        session = FakeSession(
            [
                FakeResponse(
                    [
                        {
                            "lat": "50.1485",
                            "lon": "12.5060",
                            "category": "historic",
                            "type": "castle",
                            "name": "Kaceřov",
                            "display_name": "Kaceřov, okres Sokolov, Česko",
                            "address": {"village": "Kaceřov", "country_code": "cz"},
                        }
                    ]
                ),
                FakeResponse([]),
                FakeResponse([]),
                FakeResponse([]),
            ]
        )
        stop = gapfill.StopQuery("1", "Kaceřov", "Kaceřov", "SO", "CZ")
        with (
            TemporaryDirectory() as directory,
            patch.object(gapfill.time, "sleep"),
        ):
            candidates = gapfill.NominatimClient(
                session=session, retries=1, delay=0
            ).search(stop, Path(directory))
        self.assertEqual("poi", candidates[0].kind)

    def test_nominatim_roads_are_not_stops(self):
        for category, kind, expected in (
            ("highway", "bus_stop", True),
            ("highway", "platform", True),
            ("public_transport", "stop_position", True),
            ("railway", "halt", True),
            ("highway", "residential", False),
            ("highway", "pedestrian", False),
            ("highway", "secondary", False),
            ("building", "civic", False),
        ):
            with self.subTest(category=category, kind=kind):
                self.assertEqual(
                    expected,
                    gapfill.nominatim_item_is_stop({"category": category, "type": kind}),
                )

    def test_nominatim_continues_after_unrelated_results(self):
        unrelated = {
            "osm_type": "node",
            "osm_id": 1,
            "lat": "49.8580",
            "lon": "12.3530",
            "category": "tourism",
            "type": "hotel",
            "name": "Lodermühl Hotel",
            "display_name": "Lodermühl Hotel, Tirschenreuth, Deutschland",
            "address": {"town": "Tirschenreuth", "country_code": "de"},
        }
        transit_stop = {
            "osm_type": "node",
            "osm_id": 2,
            "lat": "49.8584449",
            "lon": "12.3538413",
            "category": "highway",
            "type": "bus_stop",
            "name": "Lodermühl",
            "display_name": "Lodermühl, Tirschenreuth, Deutschland",
            "address": {"town": "Tirschenreuth", "country_code": "de"},
        }
        session = FakeSession([FakeResponse([unrelated]), FakeResponse([transit_stop])])
        stop = gapfill.StopQuery("1", "Lodermühl", "Tirschenreuth", "", "D")
        with (
            TemporaryDirectory() as directory,
            patch.object(gapfill.time, "sleep"),
        ):
            candidates = gapfill.NominatimClient(
                session=session, retries=1, delay=0
            ).search(stop, Path(directory))

        self.assertEqual(["poi", "stop"], [candidate.kind for candidate in candidates])
        self.assertEqual(2, len(session.calls))

    def test_unique_exact_last_component_is_accepted(self):
        candidate = gapfill.Candidate(
            ("OD Tesco a Atrium",), 50.21, 15.82, "Hradec Kralove", "CZ"
        )
        self.assertEqual(
            candidate, gapfill.choose_exact_candidate(self.stop(), [candidate])
        )

    def test_nearby_platforms_are_accepted_as_stop_centroid(self):
        stop = gapfill.StopQuery("1", "Wisla,Oaza", "Wisla", "", "PL")
        candidates = [
            gapfill.Candidate(("Wisła Oaza",), 49.6479, 18.8675, "Wisła", "PL"),
            gapfill.Candidate(("Wisła Oaza",), 49.6469, 18.8681, "Wisła", "PL"),
        ]
        chosen = gapfill.choose_exact_candidate(stop, candidates)
        self.assertIsNotNone(chosen)
        self.assertAlmostEqual(49.6474, chosen.latitude, places=4)

    def test_distant_same_name_stops_remain_ambiguous(self):
        stop = gapfill.StopQuery("1", "Central", "Town", "", "D")
        candidates = [
            gapfill.Candidate(("Central",), 49.0, 12.0, "Town", "D"),
            gapfill.Candidate(("Central",), 50.0, 12.0, "Town", "D"),
        ]
        self.assertIsNone(gapfill.choose_exact_candidate(stop, candidates))

    def test_unique_largest_platform_cluster_wins_over_distant_outlier(self):
        stop = gapfill.StopQuery("1", "Jaworzynka,Skrzyzowanie", "Jaworzynka", "", "PL")
        candidates = [
            gapfill.Candidate(
                ("Jaworzynka Skrzyżowanie",),
                49.54946,
                18.88297,
                "Javořinka",
                "PL",
                rank=0,
            ),
            gapfill.Candidate(
                ("Jaworzynka Skrzyżowanie",),
                49.54916,
                18.88220,
                "Javořinka",
                "PL",
                rank=1,
            ),
            gapfill.Candidate(
                ("Jaworzynka Skrzyżowanie",),
                49.52607,
                18.85773,
                "Javořinka",
                "PL",
                rank=2,
            ),
        ]
        chosen = gapfill.choose_exact_candidate(stop, candidates)
        self.assertIsNotNone(chosen)
        self.assertGreater(chosen.latitude, 49.54)

    def test_repaired_mhd_context_matches_municipality_prefixed_candidate(self):
        stop = gapfill.StopQuery("1", "Lázně I", "Karlovy Vary", "KV", "CZ")
        candidate = gapfill.Candidate(
            ("Karlovy Vary, Lázně I",), 50.22, 12.88, "Karlovy Vary", "CZ"
        )
        self.assertEqual(candidate, gapfill.choose_exact_candidate(stop, [candidate]))

    def test_component_aliases_cover_real_mapy_stop_names(self):
        examples = [
            (
                gapfill.StopQuery(
                    "1",
                    "Horní Jiřetín,Dolní Jiřetín,odb.Centrum",
                    "Horní Jiřetín",
                    "MO",
                    "CZ",
                ),
                gapfill.Candidate(
                    ("Horní Jiřetín, Dolní Jiřetín, odbočka Centrum",),
                    50.57,
                    13.55,
                    "Horní Jiřetín",
                    "CZ",
                ),
            ),
            (
                gapfill.StopQuery("2", "Charbuzice,křiž.", "Charbuzice", "JC", "CZ"),
                gapfill.Candidate(
                    ("Charbuzice, křižovatka",), 50.22, 15.51, "Charbuzice", "CZ"
                ),
            ),
            (
                gapfill.StopQuery("3", "Stěžírky", "Stěžery", "HK", "CZ"),
                gapfill.Candidate(
                    ("Stěžery, Stěžírky",), 50.23, 15.76, "Stěžery", "CZ"
                ),
            ),
        ]
        for stop, candidate in examples:
            with self.subTest(stop=stop.name):
                self.assertEqual(
                    candidate, gapfill.choose_exact_candidate(stop, [candidate])
                )

    def test_estimated_candidate_must_pass_route_time_ceiling(self):
        stop = gapfill.StopQuery(
            "1",
            "Target",
            "Town",
            "HK",
            "CZ",
            "estimated",
            50.0,
            15.0,
            "estimated:route-time",
            1,
            (
                {
                    "previous": {
                        "stop_id": "anchor",
                        "elapsed_minutes": 2,
                        "latitude": 50.0,
                        "longitude": 15.0,
                    },
                    "following": None,
                },
            ),
        )
        nearby = gapfill.Candidate(("Target",), 50.02, 15.0, "Town", "CZ")
        distant = gapfill.Candidate(("Target",), 49.0, 14.0, "Town", "CZ")
        self.assertTrue(gapfill.candidate_geography(stop, nearby)[0])
        accepted, reason, _ = gapfill.candidate_geography(stop, distant)
        self.assertFalse(accepted)
        self.assertEqual("route_time_impossible", reason)

    def test_route_estimate_resolves_one_clearly_better_foreign_candidate(self):
        stop = replace(
            self.stop("Altenberg,Bahnhof"),
            country="D",
            coordinate_status="estimated",
            latitude=50.72,
            longitude=13.76,
            route_contexts=(),
        )
        near = gapfill.Candidate(
            ("Altenberg Bahnhof",),
            50.765,
            13.755,
            "Altenberg",
            "DE",
            "stop",
        )
        far = replace(
            near,
            names=("Dresden Flughafen",),
            latitude=50.80,
            longitude=13.68,
        )
        self.assertEqual(
            near, gapfill.choose_route_supported_candidate(stop, [near, far])
        )

    def test_exact_locality_alias_can_override_bad_jdf_municipality(self):
        stop = gapfill.StopQuery(
            "1",
            "Hradec Králové,Charbuzice křižovatka",
            "Hradec Králové",
            "HK",
            "CZ",
            "estimated",
            50.2256,
            15.7541,
            route_contexts=(
                {
                    "previous": {
                        "stop_id": "anchor",
                        "elapsed_minutes": 2,
                        "latitude": 50.22,
                        "longitude": 15.76,
                    },
                    "following": None,
                },
            ),
        )
        candidate = gapfill.Candidate(
            ("Charbuzice křižovatka",), 50.22442, 15.75035, "Stěžery", "CZ"
        )
        accepted, reason, _ = gapfill.candidate_geography(stop, candidate)
        self.assertTrue(accepted)
        self.assertEqual("route_exact_locality_alias", reason)

    def test_query_expansion_does_not_expand_an_already_full_word(self):
        self.assertEqual(
            "Charbuzice křižovatka", gapfill.expanded_query("Charbuzice křižovatka")
        )
        self.assertEqual(
            "Charbuzice křižovatka", gapfill.expanded_query("Charbuzice křiž.")
        )

    def test_estimated_endpoint_uses_one_anchor_and_no_context_uses_2km(self):
        endpoint = gapfill.StopQuery(
            "1",
            "Target",
            "Town",
            "HK",
            "CZ",
            "estimated",
            50.0,
            15.0,
            route_contexts=(
                {
                    "previous": None,
                    "following": {
                        "stop_id": "anchor",
                        "elapsed_minutes": 1,
                        "latitude": 50.0,
                        "longitude": 15.0,
                    },
                },
            ),
        )
        candidate = gapfill.Candidate(("Target",), 50.01, 15.0, "Town", "CZ")
        self.assertTrue(gapfill.candidate_geography(endpoint, candidate)[0])
        no_context = replace(endpoint, route_contexts=())
        self.assertTrue(gapfill.candidate_geography(no_context, candidate)[0])
        too_far = gapfill.Candidate(("Target",), 50.03, 15.0, "Town", "CZ")
        self.assertFalse(gapfill.candidate_geography(no_context, too_far)[0])

    def test_token_order_and_municipality_aliases_are_normalized(self):
        stop = gapfill.StopQuery(
            "1", "Bayer.Eisenstein,Local-Bahn-Museum", "Bayer.Eisenstein", "", "D"
        )
        candidate = gapfill.Candidate(
            ("Local Bahn Museum",), 49.12, 13.2, "Bayerisch Eisenstein", "DE"
        )
        self.assertEqual(candidate, gapfill.choose_exact_candidate(stop, [candidate]))

    def test_wrong_country_and_ambiguity_are_rejected(self):
        wrong = gapfill.Candidate(
            ("OD Tesco a Atrium",), 50.21, 15.82, "Hradec Králové", "DE"
        )
        first = gapfill.Candidate(
            ("OD Tesco a Atrium",), 50.21, 15.82, "Hradec Králové", "CZ"
        )
        second = gapfill.Candidate(
            ("OD Tesco a Atrium",), 50.22, 15.83, "Hradec Králové", "CZ"
        )
        self.assertIsNone(gapfill.choose_exact_candidate(self.stop(), [wrong]))
        self.assertIsNone(gapfill.choose_exact_candidate(self.stop(), [first, second]))

    def test_fuzzy_match_requires_transit_geography_and_clear_margin(self):
        stop = gapfill.StopQuery("1", "Wisla,dw.aut.", "Wisla", "", "PL")
        best = gapfill.Candidate(
            ("Wisła Dworzec Autobusowy",), 49.65, 18.86, "Wisła", "PL", "stop"
        )
        weaker = gapfill.Candidate(
            ("Wisła Centrum Turystyczne",), 49.66, 18.87, "Wisła", "PL", "stop"
        )
        poi = gapfill.Candidate(
            ("Wisła Dworzec Autobusowy",), 49.67, 18.88, "Wisła", "PL", "poi"
        )
        self.assertEqual(
            best, gapfill.choose_fuzzy_candidate(stop, [best, weaker, poi], 0.7)
        )
        competing = gapfill.Candidate(
            ("Wisła Dworzec Autobusowy 2",), 49.68, 18.89, "Wisła", "PL", "stop"
        )
        self.assertIsNone(gapfill.choose_fuzzy_candidate(stop, [best, competing], 0.7))

    def test_osm_candidate_without_municipality_can_match_same_okres(self):
        same_region = gapfill.Candidate(
            ("OD Tesco a Atrium",), 50.21, 15.82, "", "CZ", region="HK"
        )
        wrong_region = gapfill.Candidate(
            ("OD Tesco a Atrium",), 50.21, 15.82, "", "CZ", region="PA"
        )
        self.assertEqual(
            same_region, gapfill.choose_exact_candidate(self.stop(), [same_region])
        )
        self.assertIsNone(gapfill.choose_exact_candidate(self.stop(), [wrong_region]))

    def test_osm_nodes_ways_relations_and_aliases(self):
        candidates = gapfill.osm_candidates(
            [
                {"lat": 1, "lon": 2, "tags": {"name": "Node", "addr:city": "Town"}},
                {
                    "center": {"lat": 3, "lon": 4},
                    "tags": {"official_name": "Way", "addr:city": "Town"},
                },
                {
                    "center": {"lat": 5, "lon": 6},
                    "tags": {"alt_name": "Rel;Alias", "is_in": "Town"},
                },
            ]
        )
        self.assertEqual(
            [("Node",), ("Way",), ("Rel", "Alias")], [item.names for item in candidates]
        )

    def test_osm_auto_context_supplies_missing_geography(self):
        candidate = gapfill.osm_candidates(
            [{"lat": 1, "lon": 2, "tags": {"name": "KARLA"}}],
            fallback_municipality="Bruntál",
            fallback_country="CZ",
        )[0]
        self.assertEqual("Bruntál", candidate.municipality)
        self.assertEqual("CZ", candidate.country)
        self.assertEqual(
            "49.880000,14.813313,50.120000,15.186687",
            gapfill.municipality_bbox((50, 15), 0.12),
        )
        self.assertEqual(
            "14.688855,49.800000,15.311145,50.200000", gapfill.mapy_bbox((50, 15))
        )

    def test_gtfs_municipality_centers_use_jdf_country_and_region(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            gtfs = root / "gtfs"
            gtfs.mkdir()
            (gtfs / "stops.txt").write_text(
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "jdf:stop:1:unspecified,Bruntál OSRAM,49.99,17.47\n",
                encoding="utf-8",
            )
            jdf = root / "jdf.zip"
            with zipfile.ZipFile(jdf, "w") as archive:
                archive.writestr(
                    "Zastavky.txt",
                    '"1","Bruntál","OSRAM","","BR","CZ","","","","","","";\r\n'.encode(
                        "cp1250"
                    ),
                )
            centers = gapfill.gtfs_municipality_centers(gtfs, jdf)
        self.assertEqual((49.99, 17.47), centers[("CZ", "bruntal", "BR")])

    def test_review_output_can_be_used_as_the_next_gapfill_input(self):
        with TemporaryDirectory() as directory:
            accepted = Path(directory) / "accepted.csv"
            review = Path(directory) / "review.csv"
            gapfill.write_results(accepted, review, [], [(self.stop(), "not_found", 0)])
            loaded = gapfill.load_queries(review)
        self.assertEqual([self.stop()], loaded)

    def test_grouped_worklist_stop_ids_are_accepted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "worklist.csv"
            path.write_text(
                "stop_ids,name,municipality,region,country\n"
                "jdf:stop:1:unspecified,NC Černice,Plzeň,PM,CZ\n",
                encoding="utf-8",
            )
            loaded = gapfill.load_queries(path)
        self.assertEqual("jdf:stop:1:unspecified", loaded[0].stop_id)

    def test_mapy_uses_header_secret_and_parses_regional_structure(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "items": [
                            {
                                "name": "OD Tesco a Atrium",
                                "label": "Zastávka MHD",
                                "type": "poi",
                                "position": {"lat": 50.21, "lon": 15.82},
                                "regionalStructure": [
                                    {
                                        "type": "regional.municipality",
                                        "name": "Hradec Králové",
                                    },
                                    {
                                        "type": "regional.country",
                                        "name": "Česko",
                                        "isoCode": "CZ",
                                    },
                                ],
                            }
                        ]
                    }
                ),
                *[FakeResponse({"items": []}) for _ in range(10)],
            ]
        )
        client = gapfill.MapyClient("secret", session=session, retries=1)
        candidates = client.geocode(self.stop())
        self.assertEqual(1, len(candidates))
        self.assertEqual("secret", session.calls[0][2]["headers"]["X-MAPY-API-KEY"])
        self.assertNotIn("apiKey", session.calls[0][2]["params"])
        self.assertEqual("CZ", session.calls[0][2]["params"]["locality"])
        self.assertEqual("poi", session.calls[0][2]["params"]["type"])

    def test_mapy_recognizes_foreign_public_transport_labels(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "items": [
                            {
                                "name": "Stadtplatz",
                                "label": "Bushaltestelle",
                                "type": "poi",
                                "position": {"lat": 48.52, "lon": 14.29},
                                "regionalStructure": [
                                    {
                                        "type": "regional.municipality",
                                        "name": "Bad Leonfelden",
                                    },
                                    {"type": "regional.country", "isoCode": "AT"},
                                ],
                            }
                        ]
                    }
                ),
                *[FakeResponse({"items": []}) for _ in range(10)],
            ]
        )
        stop = gapfill.StopQuery(
            "1", "Bad Leonfelden,Stadtplatz", "Bad Leonfelden", "", "A"
        )
        candidate = gapfill.MapyClient("secret", session=session, retries=1).geocode(
            stop
        )[0]
        self.assertEqual("stop", candidate.kind)

    def test_mapy_never_queries_or_classifies_a_town_fallback(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "items": [
                            {
                                "name": "Bruntál",
                                "label": "Město",
                                "type": "regional.municipality",
                                "position": {"lat": 49.99, "lon": 17.46},
                                "regionalStructure": [
                                    {
                                        "type": "regional.municipality",
                                        "name": "Bruntál",
                                    },
                                    {"type": "regional.country", "isoCode": "CZ"},
                                ],
                            }
                        ]
                    }
                ),
                *[FakeResponse({"items": []}) for _ in range(10)],
            ]
        )
        stop = gapfill.StopQuery("1", "KARLA", "Bruntál", "BR", "CZ")
        candidates = gapfill.MapyClient("secret", session=session, retries=1).geocode(
            stop
        )
        self.assertEqual("poi", candidates[0].kind)
        self.assertTrue(
            all(call[2]["params"]["type"] == "poi" for call in session.calls)
        )

    def test_mapy_uses_known_municipality_center_as_bbox(self):
        session = FakeSession(
            [
                *[FakeResponse({"items": []}) for _ in range(10)],
            ]
        )
        stop = gapfill.StopQuery("1", "Loket,u dálnice", "Loket", "BN", "CZ")
        centers = {("CZ", "loket", "BN"): (49.65, 15.12)}
        gapfill.MapyClient(
            "secret", session=session, retries=1, municipality_centers=centers
        ).geocode(stop)
        params = session.calls[0][2]["params"]
        self.assertEqual("CZ", params["locality"])
        self.assertEqual("15.1200000,49.6500000", params["preferNear"])
        self.assertEqual(2000, params["preferNearPrecision"])

    def test_audit_uses_metadata_precision_and_drops_single_stop_trip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            gtfs = root / "bundle" / "gtfs-intermediate"
            gtfs.mkdir(parents=True)
            (gtfs / "stops.txt").write_text(
                "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                "jdf:stop:1,Target [?],50,15,1,\n"
                "jdf:stop:1:unspecified,Target [?],50,15,0,jdf:stop:1\n"
                "jdf:stop:2,Anchor,50,15.01,1,\n"
                "jdf:stop:2:unspecified,Anchor,50,15.01,0,jdf:stop:2\n"
                "jdf:stop:3,Not metadata tagged [?],50,15.02,1,\n"
                "jdf:stop:3:unspecified,Not metadata tagged [?],50,15.02,0,jdf:stop:3\n",
                encoding="utf-8",
            )
            (gtfs / "stop_times.txt").write_text(
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "valid,08:00:00,08:00:00,jdf:stop:1:unspecified,1\n"
                "valid,08:02:00,08:02:00,jdf:stop:2:unspecified,2\n"
                "invalid,09:00:00,09:00:00,jdf:stop:3:unspecified,1\n",
                encoding="utf-8",
            )
            (gtfs / "trips.txt").write_text(
                "route_id,service_id,trip_id\nr,service,valid\nr,service,invalid\n",
                encoding="utf-8",
            )
            (gtfs / "routes.txt").write_text(
                "route_id,route_short_name,route_long_name\nr,R,Test route\n",
                encoding="utf-8",
            )
            jdf = root / "jdf.zip"
            with zipfile.ZipFile(jdf, "w") as archive:
                archive.writestr(
                    "Zastavky.txt",
                    (
                        '"1","Town","Target","","HK","CZ","","","","","","";\r\n'
                        '"2","Town","Anchor","","HK","CZ","","","","","","";\r\n'
                        '"3","Town","Other","","HK","CZ","","","","","","";\r\n'
                    ).encode("cp1250"),
                )
            output = root / "audit.csv"
            metadata = {
                "jdf:stop:1": {
                    "coordinate_precision": "estimated",
                    "coordinate_source": "estimated:route-time",
                },
                "jdf:stop:2": {
                    "coordinate_precision": "stop",
                    "coordinate_source": "manual",
                },
                "jdf:stop:3": {
                    "coordinate_precision": "stop",
                    "coordinate_source": "manual",
                },
            }
            args = SimpleNamespace(
                gtfs=gtfs,
                jdf=jdf,
                output=output,
                metadata=None,
                coordinate_status="estimated",
                minimum_run_length=None,
                include_unresolved_termini=False,
            )
            with patch.object(gapfill, "load_stop_metadata", return_value=metadata):
                gapfill.run_audit(args)
            with output.open(encoding="utf-8") as stream:
                rows = list(gapfill.csv.DictReader(stream))
        self.assertEqual(["jdf:stop:1"], [row["stop_id"] for row in rows])
        self.assertEqual("1", rows[0]["route_occurrences"])
        self.assertEqual("1", rows[0]["maximum_consecutive_unresolved"])
        self.assertEqual("true", rows[0]["unresolved_terminus"])
        self.assertEqual("r", rows[0]["route_ids"])
        self.assertEqual(
            "jdf:stop:2",
            gapfill.json.loads(rows[0]["route_contexts"])[0]["following"]["stop_id"],
        )

    def test_audit_prioritizes_long_runs_and_unresolved_termini(self):
        calls = [
            ("anchor-a", 0),
            ("gap-1", 1),
            ("gap-2", 2),
            ("gap-3", 3),
            ("gap-4", 4),
            ("anchor-b", 5),
            ("terminal", 6),
        ]
        targets = {"gap-1", "gap-2", "gap-3", "gap-4", "terminal"}
        collapsed, impacts = gapfill.unresolved_run_impacts(calls, targets)
        self.assertEqual(calls, collapsed)
        self.assertEqual((4, False), impacts["gap-1"])
        self.assertEqual((1, True), impacts["terminal"])

    def test_mapy_errors_do_not_disclose_secret(self):
        client = gapfill.MapyClient(
            "do-not-print", session=FakeSession([FakeResponse({}, 500)]), retries=1
        )
        with self.assertRaises(RuntimeError) as raised:
            client.geocode(self.stop())
        self.assertNotIn("do-not-print", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
