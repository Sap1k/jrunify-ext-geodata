import csv
import unittest
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
        chained = [stop(1, "A", note="merged_into:2"), stop(2, "B", note="merged_into:3"), stop(3, "C")]
        self.assertTrue(any("is itself merged" in error for error in registry.validate(chained, [])))

    def test_posts_reference_registered_stops(self):
        stops = [stop(1, "A")]
        self.assertEqual(registry.validate(stops, [post(1, "est:1"), post(1, "post:3", "", "")]), [])
        errors = registry.validate(stops, [post(1, "est:0"), post(1, "est:2", "", ""), post(2, "est:1")])
        self.assertEqual(len(errors), 3)
        errors = registry.validate(stops, [post(1, "est:2"), post(1, "est:1")])
        self.assertEqual(errors, ["posts.csv must be sorted by stop_id, then post_key"])

    def write_registry(self, root, stops, posts=(), places=()):
        registry.write_table(root / "stops.csv", registry.STOP_FIELDS, list(stops))
        registry.write_table(root / "posts.csv", registry.POST_FIELDS, list(posts))
        registry.write_table(root / "overlay_places.csv", registry.PLACE_FIELDS, list(places))

    def write_candidates(self, path, fields, rows):
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, restval="")
            writer.writeheader()
            writer.writerows(rows)

    def promote(self, root, candidates, accept_new=False):
        registry.run_promote(SimpleNamespace(registry=root, candidates=candidates, accept_new=accept_new))
        return registry._load(root)

    STOP_CANDIDATE_FIELDS = [
        "provisional_id", "town", "district", "nearby_place", "okres", "country",
        "lat", "lon", "reason", "alias_of_suggestion", "decision", "alias_of",
    ]

    def test_promote_allocates_ids_and_aliases(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "registry"
            self.write_registry(root, [stop(5, "Babice", "", "pošta")])
            candidates = Path(directory) / "candidates.csv"
            base = {"district": "", "okres": "UH", "country": "CZ", "reason": "new"}
            self.write_candidates(candidates, self.STOP_CANDIDATE_FIELDS, [
                {**base, "provisional_id": "1000000002", "town": "Zlín", "nearby_place": "nádraží", "decision": "new"},
                {**base, "provisional_id": "1000000002", "town": "Zlín", "nearby_place": "žel.st.", "decision": "new"},
                {**base, "provisional_id": "", "town": "Babice", "nearby_place": "pošta nová", "reason": "alias", "alias_of_suggestion": "5", "decision": "alias"},
                {**base, "provisional_id": "1000000001", "town": "Alpha"},
            ])
            stops, _, _ = self.promote(root, candidates)
            self.assertEqual(
                [(row["id"], row["town"], row["nearby_place"]) for row in stops],
                [("5", "Babice", "pošta"), ("5", "Babice", "pošta nová"),
                 ("6", "Zlín", "nádraží"), ("6", "Zlín", "žel.st.")],
            )
            self.assertEqual(self.promote(root, candidates)[0][:2], stops[:2])

    def test_accept_new_bootstraps_an_empty_registry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "registry"
            self.write_registry(root, [])
            candidates = Path(directory) / "candidates.csv"
            base = {"district": "", "nearby_place": "", "okres": "UH", "country": "CZ", "reason": "new"}
            self.write_candidates(candidates, self.STOP_CANDIDATE_FIELDS[:10], [
                {**base, "provisional_id": "1000000009", "town": "B"},
                {**base, "provisional_id": "1000000003", "town": "A"},
                {**base, "provisional_id": "1000000004", "town": "C", "reason": "ambiguous"},
            ])
            stops, _, _ = self.promote(root, candidates, accept_new=True)
            self.assertEqual([(row["id"], row["town"]) for row in stops], [("1", "A"), ("2", "B")])

    def test_promote_posts_and_overlay_places(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "registry"
            self.write_registry(
                root,
                [stop(1, "A")],
                places=[{"source_id": "pid", "group_key": "group:x:old", "place_id": "overlay:pid:stop-place:aa", "status": "active", "note": ""}],
            )
            posts = Path(directory) / "posts.csv"
            self.write_candidates(posts, ["stop_id", "post_key", "lat", "lon", "reason"], [
                {"stop_id": "1", "post_key": "est:2", "lat": "49.1", "lon": "17.4", "reason": "new"},
                {"stop_id": "1", "post_key": "est:1", "lat": "49.2", "lon": "17.4", "reason": "new"},
            ])
            _, stored_posts, _ = self.promote(root, posts, accept_new=True)
            self.assertEqual([row["post_key"] for row in stored_posts], ["est:1", "est:2"])
            places = Path(directory) / "places.csv"
            self.write_candidates(places, ["source_id", "group_key", "place_id", "stop_name", "lat", "lon", "reason", "alias_of_suggestion", "decision"], [
                {"source_id": "pid", "group_key": "group:x:new", "place_id": "overlay:pid:stop-place:bb", "stop_name": "X", "reason": "new", "alias_of_suggestion": "overlay:pid:stop-place:aa", "decision": "alias"},
                {"source_id": "pid", "group_key": "group:y:", "place_id": "overlay:pid:stop-place:cc", "stop_name": "Y", "reason": "new", "decision": "new"},
            ])
            _, _, stored_places = self.promote(root, places)
            self.assertEqual(
                [(row["group_key"], row["place_id"]) for row in stored_places],
                [("group:x:new", "overlay:pid:stop-place:aa"), ("group:x:old", "overlay:pid:stop-place:aa"), ("group:y:", "overlay:pid:stop-place:cc")],
            )

    def test_checked_registry_is_valid(self):
        stops, posts, places = registry._load(Path(registry.__file__).parent / "registry")
        self.assertEqual(registry.validate(stops, posts, places), [])


if __name__ == "__main__":
    unittest.main()
