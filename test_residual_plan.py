import unittest

import residual_plan


class ResidualPlanTests(unittest.TestCase):
    def row(self):
        return {
            "name": "Hranice,nemoc.",
            "municipality": "Hranice",
            "region": "PR",
            "country": "CZ",
        }

    def test_historical_town_name_is_not_a_border_call(self):
        self.assertEqual(
            residual_plan.stage_for("Hranice,nemoc.", "CZ"), "targeted-cz-osm"
        )
        self.assertEqual(
            residual_plan.stage_for("Záhony,ZOLL", "H"), "manual-border-call"
        )

    def test_external_match_requires_a_compact_candidate_cluster(self):
        key = (residual_plan.identity_text("Hranice,nemoc."), "CZ", "PR")
        clustered = {key: [(49.55, 17.73), (49.5502, 17.7302)]}
        scattered = {key: [(49.55, 17.73), (49.65, 17.83)]}
        self.assertTrue(residual_plan.covered_by_external(self.row(), clustered))
        self.assertFalse(residual_plan.covered_by_external(self.row(), scattered))

    def test_residual_identity_keeps_same_named_stops_in_separate_geographies(self):
        first = self.row()
        second = {**first, "municipality": "Other Hranice", "region": "NJ"}

        self.assertNotEqual(
            residual_plan.residual_identity_key(first),
            residual_plan.residual_identity_key(second),
        )

    def test_empty_external_identity_mapping_is_supported(self):
        self.assertFalse(residual_plan.covered_by_external(self.row(), {}))


if __name__ == "__main__":
    unittest.main()
