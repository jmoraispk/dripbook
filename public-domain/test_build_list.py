"""Offline tests for build_list.py: python3 -m unittest discover -s public-domain

Gutendex is never called: books are made up here.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_list  # noqa: E402

CUTOFF = build_list.cutoff_year(2026)


def book(id=1, title="Walden", authors=(("Thoreau, Henry David", 1862),), translators=(), copyright=False,
         subjects=(), shelves=("Category: Philosophy", "Harvard Classics"), born=1817):
    def people(ps):
        return [{"name": n, "birth_year": born, "death_year": d} for n, d in ps]
    return {"id": id, "title": title, "authors": people(authors), "translators": people(translators),
            "copyright": copyright, "media_type": "Text", "download_count": 10,
            "subjects": list(subjects), "bookshelves": list(shelves)}


class CheckTest(unittest.TestCase):
    def test_cutoff_in_2026_is_1955(self):
        self.assertEqual(CUTOFF, 1955)

    def test_old_author_is_kept(self):
        self.assertIsNone(build_list.check(book(), CUTOFF))

    def test_author_who_died_after_1955_is_left_out(self):
        self.assertIn("too recently", build_list.check(book(authors=(("Hemingway, Ernest", 1961),)), CUTOFF))

    def test_recent_translator_is_left_out(self):
        art_of_war = book(title="The Art of War", authors=(("Sunzi", -496),), translators=(("Giles, Lionel", 1958),))
        self.assertIn("too recently", build_list.check(art_of_war, CUTOFF))

    def test_unknown_death_year_is_left_out(self):
        self.assertIn("no known death year", build_list.check(book(authors=(("Various", None),)), CUTOFF))

    def test_us_copyright_is_left_out(self):
        self.assertIn("US", build_list.check(book(copyright=None), CUTOFF))

    def test_one_volume_of_a_set_is_left_out(self):
        for title in ["The Works of William Shakespeare [Cambridge Edition] [Vol. 5 of 9]",
                      "The History of Rome, Volume II", "Les Misérables, Part 1"]:
            self.assertIn("multi-volume", build_list.check(book(title=title), CUTOFF), title)
        self.assertIsNone(build_list.check(book(title="Volume and Value"), CUTOFF))


class TranslatorFlagTest(unittest.TestCase):
    def test_ancient_author_without_translator_is_flagged(self):
        self.assertTrue(build_list.check_translator(book(authors=(("Marcus Aurelius", 180),), born=121)))

    def test_foreign_literature_without_translator_is_flagged(self):
        self.assertTrue(build_list.check_translator(book(subjects=["French fiction"])))

    def test_named_translator_or_english_book_is_not_flagged(self):
        self.assertFalse(build_list.check_translator(book(subjects=["French fiction"], translators=(("Garnett", 1946),))))
        self.assertFalse(build_list.check_translator(book(subjects=["England -- Fiction"])))


class CategoryTest(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(build_list.category(book(shelves=["Category: Philosophy & Ethics"])), "Philosophy & self-development")
        self.assertEqual(build_list.category(book(shelves=["Category: Novels"])), "Literature & classics")
        self.assertEqual(build_list.category(book(shelves=[], subjects=["Cooking"])), "Other")


class BuildTest(unittest.TestCase):
    def test_keeps_one_edition_per_work_and_stops_at_count(self):
        books = [book(1, "Walden"), book(2, "Walden; or, Life in the Woods"), book(3, "Hemingway", (("H", 1961),)),
                 book(4, "Civil Disobedience"), book(5, "Excursions")]
        rows, skipped = build_list.build(books, 2, CUTOFF)
        self.assertEqual([r["gutenberg_id"] for r in rows], [1, 4])
        self.assertEqual(skipped["another edition of a book already listed"], 1)

    def test_row_format(self):
        r = build_list.row(1, book())
        self.assertEqual(r["authors"], "Thoreau, Henry David (1817-1862)")
        self.assertEqual(r["eu_public_domain_since"], 1933)
        self.assertEqual(r["gutenberg_categories"], "Philosophy")
        self.assertEqual(r["category"], "Philosophy & self-development")
        self.assertEqual(r["url"], "https://www.gutenberg.org/ebooks/1")


if __name__ == "__main__":
    unittest.main()
