import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import download


class DownloadTests(unittest.TestCase):
    def test_known_bad_karlovy_kacerov_point_is_excluded(self):
        self.assertTrue(
            download.is_known_bad_karlovarsky_point(
                "Kaceřov", 50.152037437224195, 12.532398193488628
            )
        )
        self.assertFalse(
            download.is_known_bad_karlovarsky_point(
                "Kaceřov", 50.14707555021319, 12.508229722605279
            )
        )

    def test_country_codes_are_normalized_to_jdf_values(self):
        self.assertEqual("D", download.normalize_country_code("DE"))
        self.assertEqual("A", download.normalize_country_code("at"))
        self.assertEqual("CZ", download.normalize_country_code("CZ"))
        self.assertEqual("PL", download.normalize_country_code("PL"))
        observed_iso = {
            "AT": "A",
            "BE": "B",
            "DE": "D",
            "EE": "EST",
            "ES": "E",
            "FR": "F",
            "HU": "H",
            "IT": "I",
            "LI": "FL",
            "ME": "MNE",
            "NO": "N",
            "RS": "SRB",
            "SE": "S",
            "SI": "SLO",
        }
        self.assertEqual(
            observed_iso,
            {code: download.normalize_country_code(code) for code in observed_iso},
        )
        self.assertEqual(
            "D", download.normalize_country_code(download.normalize_country_code("DE"))
        )
        self.assertEqual("DE", download.iso_country_code("D"))
        self.assertEqual("DE", download.iso_country_code("DE"))
        self.assertEqual("CZ", download.iso_country_code("CZ"))

    def test_writer_uses_the_stop_only_five_column_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "stops.csv"
            download.write_stops_csv(
                output,
                [download.Stop("Stop", 50.0, 14.0, "AB", "DE")],
            )
            with output.open(newline="") as stream:
                rows = list(csv.reader(stream))

        self.assertEqual(["Stop", "50.0", "14.0", "AB", "D"], rows[0])

    def test_known_town_is_added_once(self):
        stops = [
            download.Stop("Fibichova", 49.0, 17.0, "OC", "CZ"),
            download.Stop("Olomouc,Tržnice", 49.0, 17.0, "OC", "CZ"),
        ]
        names = [stop.name for stop in download.add_known_town(stops, "Olomouc")]
        self.assertEqual(["Olomouc,Fibichova", "Olomouc,Tržnice"], names)

    def test_dpmlj_zones_expand_to_jdf_name_components(self):
        cases = [
            ({"zone_id": "1"}, "Americká", "Liberec,,Americká"),
            ({"zone_id": "2"}, "Brandl", "Jablonec n.Nisou,,Brandl"),
            (
                {"zone_id": "2"},
                "Jablonec n.N., Tyršovy sady",
                "Jablonec n.Nisou,,Tyršovy sady",
            ),
            (
                {"zone_id": "1, 2"},
                "Vratislavická kyselka",
                "Liberec,Vratislavice n.Nisou,Vratislavická kyselka",
            ),
            (
                {"zone_id": "2, 1"},
                "Nový Svět",
                "Jablonec n.Nisou,Proseč n.Nisou,Nový Svět",
            ),
        ]
        for row, name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, download.dpmlj_stop_name(row, name))

    def test_arcgis_multipoint_yields_every_post(self):
        page = {
            "features": [
                {
                    "attributes": {"NAZ_ZAS": "Albrechtice,,střed"},
                    "geometry": {"points": [[18.525, 49.785], [18.526, 49.786]]},
                },
                {"attributes": {"NAZ_ZAS": "Bohumín"}, "geometry": {"x": 18.3, "y": 49.9}},
            ]
        }
        responses = [mock.Mock(json=mock.Mock(return_value=value)) for value in (page, {"features": []})]
        with mock.patch.object(download.requests, "post", side_effect=responses):
            stops = list(download.arcgis_download_stops("https://example.invalid", 0, ["NAZ_ZAS"]))
        self.assertEqual(
            [("Albrechtice,,střed", 49.785, 18.525), ("Albrechtice,,střed", 49.786, 18.526), ("Bohumín", 49.9, 18.3)],
            [(stop.name, stop.lat, stop.lon) for stop in stops],
        )

    def test_dpmlj_unknown_zone_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "Unknown DPMLJ zone"):
            download.dpmlj_stop_name({"zone_id": "3"}, "Stop")


if __name__ == "__main__":
    unittest.main()
