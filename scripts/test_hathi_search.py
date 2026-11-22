"""Offline tests for hathi_search.py: python3 -m unittest discover -s scripts -p 'test_hathi_search.py'"""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

import hathi_search as hs

FIELDS = ["htid", "rights", "bib", "oclc", "title", "imprint", "year", "author"]
BOOKS = [["uc1.a", "pd", "001", "", "Dracula / by Bram Stoker.", "London", "1897", "Stoker, Bram, 1847-1912."],
         ["uc1.b", "pdus", "002", "", "The jewel of seven stars", "London", "1903", "Stoker, Bram, 1847-1912."],
         ["uc1.c", "pd", "003", "", "Dracula's guest, and other weird stories", "London", "1914", "Stoker, Bram"],
         ["uc1.d", "pd", "004", "", "Émile, ou De l'éducation", "Paris", "1762", "Rousseau, Jean-Jacques"],
         ["uc1.e", "pd", "005", "", "The history of England", "London", "1848", "Macaulay, Thomas Babington"]]


class Search(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        src = Path(cls.tmp.name) / "hathi.tsv.gz"
        with gzip.open(src, "wt", encoding="utf-8") as f:
            f.write("\n".join("\t".join(r) for r in [FIELDS] + BOOKS) + "\n")
        cls.out = Path(cls.tmp.name) / "out"
        hs.build(src, cls.out, version="v1", block=60)    # small blocks, so words land in different ones

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def find(self, q):
        return [r["id"] for r in hs.search(q, self.out)]

    def test_every_word_in_the_title_or_author_shortest_title_first(self):
        self.assertEqual(self.find("stoker dracula"), ["hathi:uc1.a", "hathi:uc1.c"])
        self.assertEqual(self.find("bram"), ["hathi:uc1.a", "hathi:uc1.b", "hathi:uc1.c"])
        self.assertEqual(self.find("london"), [], "the publisher isn't searched")
        self.assertEqual(self.find("dracula vampire"), [])

    def test_small_words_and_accents(self):
        self.assertEqual(self.find("the history of england"), ["hathi:uc1.e"])
        self.assertEqual(self.find("emile education"), ["hathi:uc1.d"])
        self.assertEqual(self.find("Émile"), ["hathi:uc1.d"])

    def test_the_files(self):
        top = json.loads((self.out / "top.json").read_text())
        self.assertEqual(top["v"], "v1")
        self.assertGreater(len(top["words"]), 3)
        self.assertEqual(top["words"], sorted(top["words"]))
        size = (self.out / "v1" / "dict.txt").stat().st_size
        self.assertEqual(top["at"][-1] + top["len"][-1], size, "the blocks cover the whole dictionary")
        row = hs.search("jewel", self.out)[0]
        self.assertEqual((row["year"], row["url"]), ("1903", "https://catalog.hathitrust.org/Record/002"))

    def test_varints(self):
        ids = [0, 5, 127, 128, 300, 70000, 2 ** 31]
        self.assertEqual(hs.unvarints(hs.varints(ids)), ids)


if __name__ == "__main__":
    unittest.main()
