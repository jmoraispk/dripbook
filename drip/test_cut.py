"""Offline tests for drip/cut.py. Run: python3 -m unittest discover -s drip"""
import unittest

from cut import clean, context, cut, cut_blocks, metadata, paragraphs, part_heading, part_words, to_html, to_text, words
from gutenberg_html import blocks

RAW = """﻿The Project Gutenberg eBook of Test Book\r
\r
Title: Test Book\r
Author: Ann Writer\r
\r
*** START OF THE PROJECT GUTENBERG EBOOK TEST BOOK ***\r
\r
Produced by Somebody and the Online Distributed Proofreading Team\r
\r
There are several editions of this ebook in the Project Gutenberg collection.\r
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


class FrontMatterTest(unittest.TestCase):
    CONTENTS = "\n\n".join(["MY NOVEL", "by A. Writer", "CONTENTS", "CHAPTER I.", "CHAPTER II.", "PREFACE."])

    def book(self, front_words=300, chapters=10):
        body = [f"CHAPTER {c}.\n\n" + "\n\n".join([prose(200)] * 5) for c in ("I", "II", "III", "IV", "V",
                "VI", "VII", "VIII", "IX", "X")[:chapters]]
        return "\n\n".join([self.CONTENTS, prose(front_words)] + body)

    def test_part_one_opens_on_chapter_one(self):
        parts = cut(self.book(), 5)
        self.assertEqual(parts[0][0], ("h", "CHAPTER I."))
        self.assertEqual(sum(part_words(p) for p in parts), 10 * 1000 + 10 * 2)

    def test_book_one_heading_is_kept(self):
        text = self.book().replace("CHAPTER I.\n\n" + prose(200), "BOOK I.\n\nCHAPTER I.\n\n" + prose(200), 1)
        self.assertEqual([t for _, t in cut(text, 5)[0][:2]], ["BOOK I.", "CHAPTER I."])

    def test_long_front_matter_is_kept(self):
        parts = cut(self.book(front_words=1500), 5)   # over 5% of the book: likely the author's prologue
        self.assertEqual(parts[0][0], ("p", "MY NOVEL"))

    def test_skip_front_false_keeps_everything(self):
        self.assertEqual(cut(self.book(), 5, skip_front=False)[0][0], ("p", "MY NOVEL"))

    def test_heading_inside_an_illustration_survives(self):
        text = clean("*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\n[Illustration: TITLE\n\n\nChapter I.]\n\n"
                     "It was a truth.\n\n*** END OF THE PROJECT GUTENBERG EBOOK X ***")
        self.assertEqual(text, "Chapter I.\n\nIt was a truth.")


class HtmlTest(unittest.TestCase):
    def test_escapes_and_italics(self):
        out = to_html(paragraphs("CHAPTER I.\n\nFish & _chips_ <now>\n\none\ntwo"))
        self.assertIn("<h2", out)
        self.assertIn("Fish &amp; <em>chips</em> &lt;now&gt;", out)
        self.assertIn("one<br>two", out)

    def test_quotes_share_one_blockquote_and_pages_stay_hidden(self):
        out = to_html([("p", "Thus write,"), ("quote", "red"), ("quote", "blue"), ("pg", "7"), ("p", "Done.")])
        self.assertEqual(out.count("<blockquote"), 1)
        self.assertNotIn("7", out)
        self.assertEqual(to_text([("quote", "a &amp; b"), ("pg", "7")]), "    a & b")


class PlainQuotesTest(unittest.TestCase):
    def test_indented_lines_are_quotes(self):
        text = "\n\n".join([prose(30), "  red, white, and blue", prose(30), prose(30)])
        self.assertEqual([k for k, _ in paragraphs(text)], ["p", "quote", "p", "p"])

    def test_not_when_the_whole_book_is_indented(self):
        text = "\n\n".join("  " + prose(30).replace("\n", "\n  ") for _ in range(4))
        self.assertEqual({k for k, _ in paragraphs(text)}, {"p"})


PAGE = """<html><head><style>p {}</style></head><body>
<header id="pg-header"><h2>The Project Gutenberg eBook of Rules</h2><p>Licence.</p></header>
<p class="toc"><a href="#ch1" class="pginternal">Chapter I</a></p>
<table><tr><td>I.</td><td>Introductory</td><td>5</td></tr><tr><td>II.</td><td>Usage</td><td>7</td></tr></table>
<h1>RULES</h1>
<span class="pagenum"><a id="Page_7">{7}</a></span>
<h2>II. ELEMENTARY RULES OF USAGE</h2>
<h3><a id="Rule_2">2.</a> Use a comma.</h3>
<p>Thus <i>write</i>,</p>
<div class="example"><p>red, white, and blue</p><p>gold, silver, or copper</p></div>
<div class="figcenter"><img src="x.jpg" alt=""><p class="caption">A picture</p></div>
<p>This rule<span class="pagenum" title="8"></span> is easy.</p>
<table><tr><td>Young, the task seemed easy.</td><td>Young, I thought the task easy.</td></tr></table>
<div class="poem"><div class="stanza"><span class="i0">Line one,<br></span><span class="i0">line two.<br></span></div>
<div class="stanza"><div class="verse">Line three.</div></div></div>
<footer id="pg-footer"><p>More licence.</p></footer>
</body></html>"""


class GutenbergHtmlTest(unittest.TestCase):
    def test_blocks(self):
        self.assertEqual(blocks(PAGE), [
            ("h1", "RULES"), ("pg", "7"), ("h2", "II. ELEMENTARY RULES OF USAGE"), ("h3", "2. Use a comma."),
            ("p", "Thus <em>write</em>,"), ("quote", "red, white, and blue"), ("quote", "gold, silver, or copper"),
            ("p", "This rule is easy."), ("pg", "8"), ("quote", "Young, the task seemed easy."),
            ("quote", "Young, I thought the task easy."), ("verse", "Line one,\nline two."), ("verse", "Line three.")])


class ContextTest(unittest.TestCase):
    def test_trail_and_pages(self):
        book = [("h1", "Rules"), ("pg", "1"), ("h2", "I. Intro"), ("p", prose(1200)), ("pg", "2"),
                ("h2", "II. Usage"), ("h3", "1. Commas"), ("p", prose(1200)), ("pg", "3"), ("p", prose(1200))]
        parts = cut_blocks(book, 5, skip_front=False)
        self.assertEqual(len(parts), 3)
        self.assertEqual(context(parts, 1, "Rules"), {"trail": ["I. Intro"], "pages": ("1", "2")})
        self.assertEqual(context(parts, 2, "Rules"), {"trail": ["II. Usage", "1. Commas"], "pages": ("2", "3")})
        self.assertEqual(context(parts, 3, "Rules"), {"trail": ["II. Usage", "1. Commas"], "pages": ("3", "3")})

    def test_no_pages(self):
        parts = cut(prose(100), 5)
        self.assertIsNone(context(parts, 1)["pages"])


if __name__ == "__main__":
    unittest.main()
