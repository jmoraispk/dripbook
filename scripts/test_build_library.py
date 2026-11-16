"""Offline tests for build_library.py: python3 -m unittest discover -s scripts -p 'test_build_library.py'"""
import unittest

import build_library as bl


class Names(unittest.TestCase):
    def test_short_title(self):
        self.assertEqual(bl.short_title("Moby Dick; Or, The Whale"), "Moby Dick")
        self.assertEqual(bl.short_title("The divine comedy"), "The Divine Comedy")

    def test_author_name(self):
        self.assertEqual(bl.author_name("Austen, Jane (1775-1817)"), "Jane Austen")
        self.assertEqual(bl.author_name("Augustine, of Hippo, Saint (354-430)"), "Augustine of Hippo")
        self.assertEqual(bl.author_name("Tolstoy, Leo, graf (1828-1910)"), "Leo Tolstoy")


class Blurb(unittest.TestCase):
    def test_drops_opener_and_keeps_year(self):
        year, text = bl.blurb('"Emma" by Jane Austen is a novel published in 1815. It follows Emma. '
                              '(This is an automatically generated summary.)')
        self.assertEqual((year, text), (1815, "It follows Emma."))

    def test_titles_do_not_end_sentences(self):
        _, text = bl.blurb('"X" by Y is a novel. When wealthy Mr. Darcy arrives, things change. ' + "Z. " * 200)
        self.assertTrue(text.startswith("When wealthy Mr. Darcy arrives, things change."))


class Genres(unittest.TestCase):
    def test_shelf_and_subject(self):
        self.assertEqual(bl.genres_of(["Category: Romance"], []), ["Romance"])
        self.assertIn("Mystery & crime", bl.genres_of([], ["Detective and mystery stories -- Fiction"]))
        self.assertEqual(bl.genres_of([], ["Whaling -- Fiction"]), [])


if __name__ == "__main__":
    unittest.main()
