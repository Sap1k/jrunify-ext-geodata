import csv

import sr70_download


def test_write_pair_preserves_raw_name20(tmp_path):
    rows = [
        sr70_download.Sr70Row(
            "533398",
            "Duchcov",
            "Duchcov z",
            50.61737,
            13.755146,
            61,
        ),
        sr70_download.Sr70Row(
            "541292",
            "Háj u Duchcova",
            "Háj u Duchcova nz",
            50.636308,
            13.731383,
            61,
        ),
    ]

    sr70_download.write_pair(tmp_path, rows)

    with (tmp_path / "SR70.csv").open(encoding="utf-8", newline="") as stream:
        full_names = list(csv.reader(stream))
    with (tmp_path / "SR70_Nazev20.csv").open(encoding="utf-8", newline="") as stream:
        short_names = list(csv.reader(stream))

    assert [row[0] for row in full_names] == ["533398", "541292"]
    assert [row[1] for row in full_names] == ["Duchcov", "Háj u Duchcova"]
    assert [row[1] for row in short_names] == ["Duchcov z", "Háj u Duchcova nz"]
    assert all(len(row) == 4 for row in full_names + short_names)


def test_code_rejects_non_integral_and_non_six_digit_values():
    assert sr70_download._code(3012) == "003012"
    assert sr70_download._code(3012.0) == "003012"

    for value in (3012.5, "1234567", "ABC"):
        try:
            sr70_download._code(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected invalid identifier: {value!r}")
