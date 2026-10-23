"""
Checks for the parts of the pipeline that read documents. Run:

    python -m unittest discover tests

They use the package text already saved in data/packages/, so they need no
network and no OCR.
"""
import gzip
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import package_extract  # noqa: E402
import package_index  # noqa: E402
import update_bylaws  # noqa: E402


def package_pages(date):
    return gzip.open(ROOT / "data" / "packages" / f"{date}.txt.gz", "rt", encoding="utf-8").read().split("\f")


class FindRanges(unittest.TestCase):
    """By-laws cut out of agenda packages, checked by hand against the PDFs."""

    CASES = [
        ("2025-12-02", "2025-35", (26, 27)),   # council pay: Schedule A labelled at the page foot
        ("2025-01-21", "2025-06", (20, 21)),
        ("2022-09-20", "2022-42", (11, 23)),   # long by-law with schedules
        ("2024-12-03", "2024-54", (12, 13)),
    ]

    def test_known_bylaws(self):
        for date, number, pages in self.CASES:
            with self.subTest(number=number):
                text = package_pages(date)
                found = package_extract.find_ranges(text, lambda i: text[i], [number])
                self.assertIn(number, found)
                s, e = found[number]
                self.assertEqual((s + 1, e + 1), pages)


class Segment(unittest.TestCase):
    def test_index_matches_rules(self):
        """package_index.json is what the current rules produce from the saved
        text. If a rule changes, run: python scripts/package_index.py --resegment"""
        index = json.loads((ROOT / "data/canonical/package_index.json").read_text(encoding="utf-8"))
        stale = [d for d, e in index["packages"].items()
                 if (ROOT / "data/packages" / f"{d}.txt.gz").exists()
                 and package_index.segment(package_pages(d)) != e["items"]]
        self.assertEqual(stale, [], "run scripts/package_index.py --resegment")

    def test_kinds_found(self):
        items = package_index.segment(package_pages("2025-11-18"))
        kinds = {i["kind"] for i in items}
        self.assertIn("report", kinds)
        self.assertTrue(all(i["pages"][0] <= i["pages"][1] for i in items))


class ReportHeaders(unittest.TestCase):
    def test_column_layout(self):
        """Scanned memo headers read as a column of labels, then the values."""
        items = [i for i in package_index.segment(package_pages("2026-10-20")) if i["kind"] == "report"]
        got = [(i["pages"][0], i.get("title"), i.get("from")) for i in items]
        self.assertIn((17, "Chapman's Landing Dock", "Dan Macinnis, Operations Superintendent"), got)
        self.assertIn((16, "Year-End Report on Capital Projects", "Dan Macinnis"), got)
        self.assertIn((18, "2023-2026 Strategic Plan Final Update", None), got)

    def test_running_heads_continue_the_report(self):
        items = [i for i in package_index.segment(package_pages("2024-03-05")) if i["kind"] == "report"]
        self.assertIn([11, 16], [i["pages"] for i in items])     # Blue Box Program Transition, pages 2-6


MINUTES = """
R2025-101 A. Smith, B. Jones: That the minutes of the regular meeting be adopted. Carried.
R2025-102 A. Smith, B. Jones: That council approve the purchase as approved by
Resolution R2024-208 and in By-Law No. 2025-12. Carried.
R2025-103 B. Jones, A. Smith: That the sand be delivered to the community centre. Defeated.
R2025-104 B. Jones, A. Smith: That the item be brought back to the next meeting. Deferred.
"""


class ResolutionParsing(unittest.TestCase):
    def setUp(self):
        self.res = {r["number"]: r for r in update_bylaws.parse_resolutions_from_minutes(
            MINUTES, {"date": "2025-05-13", "minutes_url": "x.pdf", "package_url": "p.pdf"})}

    def test_reference_does_not_split(self):
        self.assertEqual(sorted(self.res), ["R2025-101", "R2025-102", "R2025-103", "R2025-104"])
        self.assertIn("R2024-208", self.res["R2025-102"]["motion_text"])

    def test_status(self):
        self.assertEqual(self.res["R2025-101"]["status"], "carried")
        self.assertEqual(self.res["R2025-103"]["status"], "defeated")
        self.assertEqual(self.res["R2025-104"]["status"], "deferred")

    def test_mover_and_bylaw(self):
        r = self.res["R2025-102"]
        self.assertEqual((r["mover"], r["seconder"]), ("A. Smith", "B. Jones"))
        self.assertEqual(r["bylaw_number"], "2025-12")


if __name__ == "__main__":
    unittest.main()
