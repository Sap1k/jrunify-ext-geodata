import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

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

    def test_context_override_replaces_bad_derived_municipality(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "context.csv"
            path.write_text(
                "stop_id,municipality,reason\n42,Most,Čepirohy is part of Most\n",
                encoding="utf-8",
            )
            query = gapfill.StopQuery("42", "Čepirohy,okály", "Čepirohy", "MO", "CZ")
            self.assertEqual(
                "Most", gapfill.apply_context_overrides([query], path)[0].municipality
            )

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
                            "namedetails": {"name": "Lodermühl"},
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
        self.assertEqual(candidates[0], gapfill.choose_exact_candidate(stop, candidates))

    def test_nominatim_does_not_treat_arbitrary_poi_as_town(self):
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
                )
            ]
        )
        stop = gapfill.StopQuery("1", "Kaceřov", "Kaceřov", "SO", "CZ")
        with TemporaryDirectory() as directory:
            candidates = gapfill.NominatimClient(
                session=session, retries=1, delay=0
            ).search(stop, Path(directory))
        self.assertEqual("poi", candidates[0].kind)

    def test_unique_exact_last_component_is_accepted(self):
        candidate = gapfill.Candidate(
            ("OD Tesco a Atrium",), 50.21, 15.82, "Hradec Kralove", "CZ"
        )
        self.assertEqual(candidate, gapfill.choose_exact_candidate(self.stop(), [candidate]))

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
                ("Jaworzynka Skrzyżowanie",), 49.54946, 18.88297, "Javořinka", "PL", rank=0
            ),
            gapfill.Candidate(
                ("Jaworzynka Skrzyżowanie",), 49.54916, 18.88220, "Javořinka", "PL", rank=1
            ),
            gapfill.Candidate(
                ("Jaworzynka Skrzyżowanie",), 49.52607, 18.85773, "Javořinka", "PL", rank=2
            ),
        ]
        chosen = gapfill.choose_exact_candidate(stop, candidates)
        self.assertIsNotNone(chosen)
        self.assertGreater(chosen.latitude, 49.54)

    def test_town_candidate_can_be_disambiguated_by_jdf_region(self):
        stop = gapfill.StopQuery("1", "Březina,škola", "Březina", "BK", "CZ")
        candidates = [
            gapfill.Candidate(("Březina",), 49.2816, 16.7495, "Březina", "CZ", "town", "BK"),
            gapfill.Candidate(("Březina",), 50.4343, 15.3133, "Březina", "CZ", "town", "JC"),
        ]
        self.assertEqual(candidates[0], gapfill.choose_town_candidate(stop, candidates))

    def test_repaired_mhd_context_matches_municipality_prefixed_candidate(self):
        stop = gapfill.StopQuery("1", "Lázně I", "Karlovy Vary", "KV", "CZ")
        candidate = gapfill.Candidate(
            ("Karlovy Vary, Lázně I",), 50.22, 12.88, "Karlovy Vary", "CZ"
        )
        self.assertEqual(candidate, gapfill.choose_exact_candidate(stop, [candidate]))

    def test_token_order_and_municipality_aliases_are_normalized(self):
        stop = gapfill.StopQuery(
            "1", "Bayer.Eisenstein,Local-Bahn-Museum", "Bayer.Eisenstein", "", "D"
        )
        candidate = gapfill.Candidate(
            ("Local Bahn Museum",), 49.12, 13.2, "Bayerisch Eisenstein", "DE"
        )
        self.assertEqual(candidate, gapfill.choose_exact_candidate(stop, [candidate]))

    def test_wrong_country_and_ambiguity_are_rejected(self):
        wrong = gapfill.Candidate(("OD Tesco a Atrium",), 50.21, 15.82, "Hradec Králové", "DE")
        first = gapfill.Candidate(("OD Tesco a Atrium",), 50.21, 15.82, "Hradec Králové", "CZ")
        second = gapfill.Candidate(("OD Tesco a Atrium",), 50.22, 15.83, "Hradec Králové", "CZ")
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
        self.assertEqual(best, gapfill.choose_fuzzy_candidate(stop, [best, weaker, poi], 0.7))
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

    def test_foreign_town_lookup_is_memoized_and_uses_town_precision(self):
        class FakeNominatim:
            def __init__(self):
                self.calls = []

            def municipality(self, name, country, cache):
                self.calls.append((name, country, cache))
                return (50.0, 14.0)

        client = FakeNominatim()
        memo = {}
        stop = gapfill.StopQuery("1", "Dresden,Hbf", "Dresden", "", "D")
        first = gapfill.foreign_town_candidates(stop, client, gapfill.Path("cache"), memo)
        second = gapfill.foreign_town_candidates(stop, client, gapfill.Path("cache"), memo)
        self.assertEqual(first, second)
        self.assertEqual(1, len(client.calls))
        self.assertEqual("town", first[0].kind)

    def test_osm_nodes_ways_relations_and_aliases(self):
        candidates = gapfill.osm_candidates(
            [
                {"lat": 1, "lon": 2, "tags": {"name": "Node", "addr:city": "Town"}},
                {"center": {"lat": 3, "lon": 4}, "tags": {"official_name": "Way", "addr:city": "Town"}},
                {"center": {"lat": 5, "lon": 6}, "tags": {"alt_name": "Rel;Alias", "is_in": "Town"}},
            ]
        )
        self.assertEqual([("Node",), ("Way",), ("Rel", "Alias")], [item.names for item in candidates])

    def test_osm_auto_context_supplies_missing_geography(self):
        candidate = gapfill.osm_candidates(
            [{"lat": 1, "lon": 2, "tags": {"name": "KARLA"}}],
            fallback_municipality="Bruntál",
            fallback_country="CZ",
        )[0]
        self.assertEqual("Bruntál", candidate.municipality)
        self.assertEqual("CZ", candidate.country)
        self.assertEqual("49.880000,14.813313,50.120000,15.186687", gapfill.municipality_bbox((50, 15), 0.12))
        self.assertEqual("14.688855,49.800000,15.311145,50.200000", gapfill.mapy_bbox((50, 15)))

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
                    '"1","Bruntál","OSRAM","","BR","CZ","","","","","","";\r\n'.encode("cp1250"),
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
                                    {"type": "regional.municipality", "name": "Hradec Králové"},
                                    {"type": "regional.country", "name": "Česko", "isoCode": "CZ"},
                                ],
                            }
                        ]
                    }
                ),
                FakeResponse({"items": []}),
                FakeResponse({"items": []}),
            ]
        )
        client = gapfill.MapyClient("secret", session=session, retries=1)
        candidates = client.geocode(self.stop())
        self.assertEqual(1, len(candidates))
        self.assertEqual("secret", session.calls[0][2]["headers"]["X-MAPY-API-KEY"])
        self.assertNotIn("apiKey", session.calls[0][2]["params"])
        self.assertEqual("Hradec Králové, CZ", session.calls[0][2]["params"]["locality"])
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
                                    {"type": "regional.municipality", "name": "Bad Leonfelden"},
                                    {"type": "regional.country", "isoCode": "AT"},
                                ],
                            }
                        ]
                    }
                ),
                FakeResponse({"items": []}),
                FakeResponse({"items": []}),
            ]
        )
        stop = gapfill.StopQuery("1", "Bad Leonfelden,Stadtplatz", "Bad Leonfelden", "", "A")
        candidate = gapfill.MapyClient("secret", session=session, retries=1).geocode(stop)[0]
        self.assertEqual("stop", candidate.kind)

    def test_mapy_explicitly_queries_municipality_for_town_fallback(self):
        session = FakeSession(
            [
                FakeResponse({"items": []}),
                FakeResponse({"items": []}),
                FakeResponse(
                    {
                        "items": [
                            {
                                "name": "Bruntál",
                                "label": "Město",
                                "type": "regional.municipality",
                                "position": {"lat": 49.99, "lon": 17.46},
                                "regionalStructure": [
                                    {"type": "regional.municipality", "name": "Bruntál"},
                                    {"type": "regional.country", "isoCode": "CZ"},
                                ],
                            }
                        ]
                    }
                ),
            ]
        )
        stop = gapfill.StopQuery("1", "KARLA", "Bruntál", "BR", "CZ")
        candidates = gapfill.MapyClient("secret", session=session, retries=1).geocode(stop)
        self.assertEqual("town", gapfill.choose_town_candidate(stop, candidates).kind)
        self.assertEqual("regional.municipality", session.calls[2][2]["params"]["type"])
        self.assertEqual("CZ", session.calls[2][2]["params"]["locality"])

    def test_bounded_top_ranked_town_breaks_same_name_ambiguity(self):
        stop = gapfill.StopQuery("1", "Loket,u dálnice", "Loket", "BN", "CZ")
        preferred = gapfill.Candidate(
            ("Loket",), 49.65, 15.12, "Loket", "CZ", "town", source="mapy-town-bbox", rank=0
        )
        other = gapfill.Candidate(
            ("Loket",), 50.18, 12.75, "Loket", "CZ", "town", source="mapy-town-bbox", rank=1
        )
        self.assertEqual(preferred, gapfill.choose_town_candidate(stop, [preferred, other]))

    def test_mapy_uses_known_municipality_center_as_bbox(self):
        session = FakeSession(
            [FakeResponse({"items": []}), FakeResponse({"items": []}), FakeResponse({"items": []})]
        )
        stop = gapfill.StopQuery("1", "Loket,u dálnice", "Loket", "BN", "CZ")
        centers = {("CZ", "loket", "BN"): (49.65, 15.12)}
        gapfill.MapyClient(
            "secret", session=session, retries=1, municipality_centers=centers
        ).geocode(stop)
        self.assertEqual(gapfill.mapy_bbox((49.65, 15.12)), session.calls[0][2]["params"]["locality"])

    def test_mapy_errors_do_not_disclose_secret(self):
        client = gapfill.MapyClient(
            "do-not-print", session=FakeSession([FakeResponse({}, 500)]), retries=1
        )
        with self.assertRaises(RuntimeError) as raised:
            client.geocode(self.stop())
        self.assertNotIn("do-not-print", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
