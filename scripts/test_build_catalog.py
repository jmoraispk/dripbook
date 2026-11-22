"""Offline tests for build_catalog.py: python3 -m unittest discover -s scripts -p 'test_build_catalog.py'"""
import unittest

import build_catalog as bc

CUTOFF = 1955


def row(id, title, authors, subjects="", shelves="", type="Text"):
    return {"Text#": str(id), "Type": type, "Title": title, "Language": "en", "Authors": authors,
            "Subjects": subjects, "Bookshelves": shelves}


class People(unittest.TestCase):
    def test_years_and_roles(self):
        self.assertEqual(bc.person("Knight, William Angus, 1836-1916 [Editor]"),
                         ("Editor", {"name": "Knight, William Angus", "birth_year": 1836, "death_year": 1916}))
        self.assertEqual(bc.person("Aesop, 621? BCE-565? BCE")[1]["death_year"], -565)
        self.assertEqual(bc.person("Bierce, Ambrose, 1842-1914?")[1]["death_year"], 1914)
        self.assertEqual(bc.person("Martorell, Joanot, -1468")[1]["death_year"], 1468)
        for unknown in ("Raymond, Eric S., 1957-", "Carter, Herbert, active 1909-1917", "Murray, John", "Various"):
            self.assertIsNone(bc.person(unknown)[1]["death_year"], unknown)


class Build(unittest.TestCase):
    def build(self, rows, library=()):
        books, _ = bc.build(rows, set(library), CUTOFF)
        return books

    def test_keeps_old_books_and_splits_the_subtitle(self):
        self.assertEqual(self.build([row(5, "Walden\nor, Life in the Woods", "Thoreau, Henry David, 1817-1862")]),
                         [[5, "Walden", "Henry David Thoreau", "or, Life in the Woods"]])

    def test_leaves_out_recent_editors_but_not_illustrators(self):
        rows = [row(1, "A", "Old, One, 1800-1850; New, Ed, 1900-1970 [Editor]"),
                row(2, "B", "Old, One, 1800-1850; New, Art, 1900-1990 [Illustrator]")]
        self.assertEqual([b[0] for b in self.build(rows)], [2])

    def test_leaves_out_library_books_their_other_editions_and_sets(self):
        rows = [row(10, "Emma", "Austen, Jane, 1775-1817"), row(20, "Emma", "Austen, Jane, 1775-1817"),
                row(30, "Clarissa, Volume 1 (of 9)", "Richardson, Samuel, 1689-1761"),
                row(40, "Persuasion", "Austen, Jane, 1775-1817")]
        self.assertEqual([b[0] for b in self.build(rows, library={10})], [40])

    def test_leaves_out_unnamed_translations(self):
        rows = [row(1, "The Odyssey", "Homer, 751? BCE-651? BCE"),
                row(2, "The Iliad", "Homer, 751? BCE-651? BCE; Butler, Samuel, 1835-1902 [Translator]")]
        self.assertEqual([b[0] for b in self.build(rows)], [2])


if __name__ == "__main__":
    unittest.main()
