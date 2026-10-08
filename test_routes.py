import unittest
from pathlib import Path

import routes


def mode(licence, agency="61974757", public_line="", expected="A", effective="E", reason="r"):
    return {
        "agency_id": agency,
        "licence": licence,
        "public_line": public_line,
        "expected_mode": expected,
        "effective_mode": effective,
        "reason": reason,
    }


def presentation(licence, agency="", short="", color="", text="", reason="r"):
    return {
        "agency_id": agency,
        "licence": licence,
        "route_short_name": short,
        "route_color": color,
        "route_text_color": text,
        "reason": reason,
    }


class LicencePatternTests(unittest.TestCase):
    def test_grammar(self):
        self.assertEqual(routes.parse_licence("915003").kind, 3)
        self.assertEqual(routes.parse_licence("915001-915019").kind, 2)
        self.assertEqual(routes.parse_licence("915*").kind, 1)
        self.assertEqual(routes.parse_licence("*").low, "")
        for value in ("91a003", "915019-915001", "9150-915019", "9*5", ""):
            with self.assertRaises(ValueError, msg=value):
                routes.parse_licence(value)

    def test_overlap(self):
        parse = routes.parse_licence
        self.assertTrue(parse("915*").overlaps(parse("915003")))
        self.assertTrue(parse("91*").overlaps(parse("915*")))
        self.assertFalse(parse("916*").overlaps(parse("915001-915019")))
        self.assertTrue(parse("915001-915019").overlaps(parse("915010-915030")))
        self.assertFalse(parse("915001-915009").overlaps(parse("915010-915030")))


class RuleValidationTests(unittest.TestCase):
    def test_checked_in_rules_are_valid(self):
        self.assertEqual(routes.validate_directory(Path(__file__).parent / "routes"), [])

    def test_liberec_replacement_buses_stay_buses(self):
        rows = routes.read_table(Path(__file__).parent / "routes" / "transport-modes.csv", routes.MODE_FIELDS)
        liberec = {row["licence"] for row in rows if row["agency_id"] == "47311975"}
        self.assertEqual(liberec, {"545002", "545003", "545004", "545005", "545011"})

    def test_mode_rows(self):
        self.assertEqual(routes.validate_modes([mode("915001-915019", public_line="1-19")]), [])
        errors = routes.validate_modes([mode("915001", expected="X", effective="A", reason="")])
        self.assertEqual(len(errors), 2)
        self.assertTrue(routes.validate_modes([mode("915001", expected="A", effective="A")]))
        self.assertTrue(routes.validate_modes([mode("915001", public_line="19-1")]))

    def test_mode_conflicts_respect_guards(self):
        disjoint = [mode("915*", public_line="1-19"), mode("915*", public_line="101-113", effective="T")]
        self.assertEqual(routes.validate_modes(disjoint), [])
        clash = [mode("915*", public_line="1-19"), mode("915*", public_line="10-30")]
        self.assertTrue(any("equally specific" in error for error in routes.validate_modes(clash)))

    def test_presentation_rows(self):
        self.assertEqual(routes.validate_presentation([presentation("915*", color="7A0200")]), [])
        errors = routes.validate_presentation([presentation("915*", color="#7a0200")])
        self.assertTrue(any("six hex digits" in error for error in errors))
        self.assertTrue(routes.validate_presentation([presentation("915*")]))

    def test_presentation_specificity(self):
        layered = [
            presentation("*", color="111111"),
            presentation("915*", color="222222"),
            presentation("915*", agency="61974757", color="333333"),
            presentation("915001-915019", color="444444"),
            presentation("915003", color="555555", short="3X"),
        ]
        self.assertEqual(routes.validate_presentation(layered), [])
        different_fields = [presentation("915*", color="111111"), presentation("915*", short="X")]
        self.assertEqual(routes.validate_presentation(different_fields), [])
        clash = [presentation("915001-915010", color="111111"), presentation("915005-915014", color="222222")]
        self.assertTrue(routes.validate_presentation(clash))


if __name__ == "__main__":
    unittest.main()
