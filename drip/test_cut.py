"""Offline tests for drip/cut.py. Run: python3 -m unittest discover -s drip"""
import unittest

from cut import clean, cut, metadata, paragraphs, part_heading, part_words, to_html, words

RAW = """﻿The Project Gutenberg eBook of Test Book\r
\r
Title: Test Book\r
Author: Ann Writer\r
\r
*** START OF THE PROJECT GUTENBERG EBOOK TEST BOOK ***\r
\r
Produced by Somebody and the Online Distributed Proofreading Team\r
\r
[Illustration:\r
\r
  A PICTURE\r
]\r
\r
CHAPTER I.\r
\r
{body}\r
\r
*** END OF THE PROJECT GUTENBERG EBOOK TEST BOOK ***\r
\r
Gutenberg licence text that must go.\r
"""


def prose(n_words, word="word"):
    """One paragraph of n_words words, hard-wrapped like Gutenberg text."""
    ws = [word] * n_words
    return "\n".join(" ".join(ws[i:i + 12]) for i in range(0, n_words, 12))


class CleanTest(unittest.TestCase):
    def test_strips_header_footer_credits_and_illustrations(self):
        text = clean(RAW.format(body="It was a dark night."))
        self.assertTrue(text.startswith("CHAPTER I."), text[:40])
        self.assertTrue(text.endswith("It was a dark night."))
        for gone in ("Gutenberg", "Produced by", "Illustration", "PICTURE", "\r", "﻿"):
            self.assertNotIn(gone, text)

    def test_metadata(self):
        self.assertEqual(metadata(RAW), ("Test Book", "Ann Writer"))


class ParagraphsTest(unittest.TestCase):
    def test_kinds(self):
        text = "CHAPTER IV.\n\n" + prose(40) + "\n\nShort line one,\nshort line two,\nand three."
        kinds = [k for k, _ in paragraphs(text)]
        self.assertEqual(kinds, ["h", "p", "verse"])

    def test_prose_is_unwrapped(self):
        (_, text), = paragraphs(prose(40))
        self.assertNotIn("\n", text)
        self.assertEqual(words(text), 40)


class CutTest(unittest.TestCase):
    def test_parts_are_near_the_target_and_keep_every_word(self):
        chapters = []
        for c in range(1, 9):
            chapters.append(f"CHAPTER {c}.")
            chapters += [prose(150)] * 12          # 1,800 words a chapter
        text = "\n\n".join(chapters)
        parts = cut(text, 10)                      # target 2,300 words
        self.assertEqual(sum(part_words(p) for p in parts), words(text))
        for p in parts[:-1]:
            self.assertGreaterEqual(part_words(p), 0.5 * 2300)
            self.assertLessEqual(part_words(p), 1.2 * 2300)
            self.assertNotEqual(p[-1][0], "h", "a part never ends on a heading")

    def test_new_chapter_starts_a_new_part_when_the_current_one_is_long_enough(self):
        text = "\n\n".join(["CHAPTER 1.", prose(1300), "CHAPTER 2.", prose(900)])
        parts = cut(text, 10)
        self.assertEqual([part_heading(p) for p in parts], ["CHAPTER 1.", "CHAPTER 2."])

    def test_short_tail_joins_the_last_part(self):
        parts = cut("\n\n".join([prose(1150)] * 2 + [prose(100)]), 5)
        self.assertEqual(len(parts), 2)
        self.assertEqual(part_words(parts[-1]), 1250)

    def test_huge_paragraph_is_split_at_sentences(self):
        sentence = " ".join(["word"] * 19) + " end."
        parts = cut(" ".join([sentence] * 300), 5)   # 6,000 words, no breaks
        self.assertGreater(len(parts), 3)
        self.assertEqual(sum(part_words(p) for p in parts), 6000)

    def test_deterministic(self):
        text = clean(RAW.format(body="\n\n".join([prose(200)] * 30)))
        self.assertEqual(cut(text, 5), cut(text, 5))


class HtmlTest(unittest.TestCase):
    def test_escapes_and_italics(self):
        out = to_html([("h", "CHAPTER I."), ("p", "Fish & _chips_ <now>"), ("verse", "one\ntwo")])
        self.assertIn("<h2", out)
        self.assertIn("Fish &amp; <em>chips</em> &lt;now&gt;", out)
        self.assertIn("one<br>two", out)


if __name__ == "__main__":
    unittest.main()
