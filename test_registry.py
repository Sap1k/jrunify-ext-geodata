import csv
import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import registry


def stop(stop_id, town, district="", nearby="", okres="UH", country="CZ", lat="", lon="", note=""):
    return {
        "id": str(stop_id),
        "town": town,
        "district": district,
        "nearby_place": nearby,
        "okres": okres,
        "country": country,
        "lat": lat,
        "lon": lon,
        "status": "active",
        "note": note,
    }


def post(stop_id, key, lat="49.1", lon="17.4"):
    return {"stop_id": str(stop_id), "post_key": key, "lat": lat, "lon": lon, "status": "active", "note": ""}


class RegistryTests(unittest.TestCase):
    def test_aliases_share_an_id(self):
        stops = [stop(1, "Babice", "", "pošta"), stop(1, "Babice", "", "pošta (old)")]
        self.assertEqual(registry.validate(stops, []), [])

    def test_same_key_needs_distinct_coordinates(self):
        far = [
            stop(1, "Babice", "", "hřbitov", lat="49.127760", lon="17.474625"),
            stop(2, "Babice", "", "hřbitov", lat="49.140000", lon="17.474625"),
        ]
        self.assertEqual(registry.validate(far, []), [])
        near = [far[0], stop(2, "Babice", "", "hřbitov", lat="49.128098", lon="17.473948")]
        self.assertTrue(any("closer than" in error for error in registry.validate(near, [])))
        bare = [stop(1, "Babice", "", "hřbitov"), stop(2, "Babice", "", "HŘBITOV")]
        self.assertTrue(any("without reference" in error for error in registry.validate(bare, [])))

    def test_rejects_provisional_range_unsorted_rows_and_bad_merges(self):
        errors = registry.validate([stop(registry.PROVISIONAL_BASE, "A")], [])
        self.assertTrue(any("outside" in error for error in errors))
        errors = registry.validate([stop(2, "B"), stop(1, "A")], [])
        self.assertIn("stops.csv must be sorted by id, then by identity", errors)
        errors = registry.validate([stop(1, "A", note="merged_into:9")], [])
        self.assertTrue(any("merged_into:9" in error for error in errors))

    def test_posts_reference_registered_stops(self):
        stops = [stop(1, "A")]
        self.assertEqual(registry.validate(stops, [post(1, "est:1"), post(1, "post:3", "", "")]), [])
        errors = registry.validate(stops, [post(1, "est:0"), post(1, "est:2", "", ""), post(2, "est:1")])
        self.assertEqual(len(errors), 3)
        errors = registry.validate(stops, [post(1, "est:2"), post(1, "est:1")])
        self.assertEqual(errors, ["posts.csv must be sorted by stop_id, then post_key"])

    def write_registry(self, root, stops):
        registry.write_table(root / "stops.csv", registry.STOP_FIELDS, stops)
        registry.write_table(root / "posts.csv", registry.POST_FIELDS, [])

    def test_promote_allocates_ids_and_aliases(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "registry"
            self.write_registry(root, [stop(5, "Babice", "", "pošta")])
            candidates = Path(directory) / "candidates.csv"
            with candidates.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=["provisional_id", "town", "district", "nearby_place", "okres", "country", "lat", "lon", "reason", "alias_of_suggestion", "decision", "alias_of"],
                )
                writer.writeheader()
                base = {"provisional_id": "1000000001", "district": "", "okres": "UH", "country": "CZ", "lat": "", "lon": "", "reason": "new", "alias_of_suggestion": ""}
                writer.writerow({**base, "town": "Zlín", "nearby_place": "nádraží", "decision": "new", "alias_of": ""})
                writer.writerow({**base, "town": "Babice", "nearby_place": "pošta nová", "decision": "alias", "alias_of": "5"})
                writer.writerow({**base, "town": "Alpha", "nearby_place": "", "decision": "", "alias_of": ""})
            args = SimpleNamespace(registry=root, candidates=candidates)
            registry.run_promote(args)
            stops, _ = registry._load(root)
            self.assertEqual(
                [(row["id"], row["town"], row["nearby_place"]) for row in stops],
                [("5", "Babice", "pošta"), ("5", "Babice", "pošta nová"), ("6", "Zlín", "nádraží")],
            )
            registry.run_promote(args)
            self.assertEqual(registry._load(root)[0], stops)

    def test_seed_keeps_export_numbers(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            jdf = directory / "merged.zip"
            with zipfile.ZipFile(jdf, "w") as archive:
                archive.writestr(
                    "Zastavky.txt",
                    '"12","Babice","","pošta","UH","CZ"\r\n"7","Zlín","","nádraží","ZL","CZ"\r\n'.encode("cp1250"),
                )
            gtfs = directory / "gtfs"
            gtfs.mkdir()
            (gtfs / "stops.txt").write_text(
                "stop_id,stop_lat,stop_lon\njdf:stop:12,49.12,17.47\njdf:stop:12:unspecified,49.12,17.47\njdf:stop:7,0,0\n",
                encoding="utf-8",
            )
            root = directory / "registry"
            registry.run_seed(SimpleNamespace(registry=root, jdf=jdf, gtfs=gtfs, metadata=None, force=False))
            stops, _ = registry._load(root)
            self.assertEqual([(row["id"], row["lat"]) for row in stops], [("7", ""), ("12", "49.120000")])
            with self.assertRaises(SystemExit):
                registry.run_seed(SimpleNamespace(registry=root, jdf=jdf, gtfs=None, metadata=None, force=False))

    def test_checked_registry_is_valid(self):
        stops, posts = registry._load(Path(registry.__file__).parent / "registry")
        self.assertEqual(registry.validate(stops, posts), [])


if __name__ == "__main__":
    unittest.main()
