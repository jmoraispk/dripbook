"""Offline tests for drip/cut.py. Run: python3 -m unittest discover -s drip"""
import unittest

from cut import (clean, context, cut, cut_blocks, drop_editorial, ends_sentence, metadata, paragraphs, part_heading,
                 part_words, to_html, to_text, words)
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
    ws[-1] += "."
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

    def test_a_part_never_stops_before_the_quotation_that_ends_its_sentence(self):
        lead = prose(1100).rstrip(".") + " and he wrote these lines upon the tomb,"
        text = "\n\n".join([lead, "    Here lies one whose name was writ in water.", prose(1100)])
        parts = cut(text, 5)                       # target 1,150 words
        self.assertIn("writ in water", to_text(parts[0]))
        self.assertTrue(to_text(parts[0]).rstrip().endswith("."))

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

    def test_long_front_matter_is_kept_but_not_the_title_page(self):
        parts = cut(self.book(front_words=1500), 5)   # over 5% of the book: likely the author's prologue
        self.assertEqual(parts[0][0], ("h", "PREFACE."))
        self.assertEqual(part_words(parts[0]) >= 1500, True)

    def test_a_plays_characters_are_kept(self):
        front = ["THE PLAY", "by A. Writer", "London: Methuen"]
        blocks = [("p", t) for t in front] + [("h2", "THE PERSONS OF THE PLAY"), ("p", "Lord Goring"),
                                              ("p", "Mabel Chiltern")] + [("p", prose(200))] * 30
        self.assertEqual(cut_blocks(blocks, 5)[0][0], ("h2", "THE PERSONS OF THE PLAY"))

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


class SweepFixesTest(unittest.TestCase):
    """What the quality sweep of the library found (2026-11-22)."""

    def test_a_footnote_number_after_the_stop_ends_a_sentence(self):
        self.assertTrue(ends_sentence([("p", "no strength to bring forth.[351]")]))
        self.assertTrue(ends_sentence([("p", "as he said.”[A]")]))
        self.assertFalse(ends_sentence([("p", "and then [12] he")]))

    def test_page_markers_in_any_span_are_page_numbers(self):
        page = ('<p>One two <span class="tei tei-pb" id="p4">[pg 4]</span> three.</p>'
                '<p><span class="left">[Page v]</span></p><p>See <a href="#p4">[pg 4]</a>.</p>')
        self.assertEqual(blocks(page), [("p", "One two three."), ("pg", "4"), ("pg", "v"), ("p", "See [pg 4].")])

    def test_a_footnote_follows_the_paragraph_that_cites_it(self):
        page = ('<p>First<a class="fnanchor" href="#Footnote_1">[1]</a> paragraph.</p><p>Second paragraph.</p>'
                '<div class="footnote"><p><a id="Footnote_1" href="#FNanchor_1">[1]</a> The note.</p>'
                '<p>Its second paragraph.</p></div>'
                '<div class="footnote"><p><a id="Footnote_2">[2]</a> Nothing cites this one.</p></div>')
        self.assertEqual(blocks(page), [("p", "First[1] paragraph."), ("note", "[1] The note."),
                                        ("note", "Its second paragraph."), ("p", "Second paragraph."),
                                        ("note", "[2] Nothing cites this one.")])

    def test_other_footnote_markup(self):
        self.assertEqual(blocks('<p>Text<a href="#n1">1</a> here.</p><p>On.</p><div class="note"><a id="n1">1</a>. His brother.</div>'),
                         [("p", "Text<sup>1</sup> here."), ("note", "1. His brother."), ("p", "On.")])
        self.assertEqual(blocks('<p>Text<a href="#f1">[1]</a>.</p><p>On.</p><p><a id="f1"></a><b>Footnote 1:</b> Anson.</p>'),
                         [("p", "Text[1]."), ("note", "<strong>Footnote 1:</strong> Anson."), ("p", "On.")])

    def test_transcribers_notes_and_credits_go(self):
        found = [("p", "E-text prepared by Chris and the Online Distributed Proofreading Team"),
                 ("p", "Please see the Transcriber’s Notes at the end of this text."), ("h2", "CHAPTER I."),
                 ("p", "It began."), ("h2", "Transcriber’s Notes"), ("p", "Fixed typos."), ("p", "More fixes.")]
        self.assertEqual(drop_editorial(found), [("h2", "CHAPTER I."), ("p", "It began.")])


class MidSentenceTest(unittest.TestCase):
    """Parts that ended mid-sentence in the second sweep (2026-11-23)."""

    def test_a_long_paragraph_splits_at_stops_not_semicolons(self):
        text = " ".join(f"Line {i} of verse runs on; and on{'.' if i % 40 == 39 else ''}" for i in range(400))
        parts = cut_blocks([("p", text)], 5)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertTrue(part[-1][1].endswith("."), part[-1][1][-30:])

    def test_a_sentence_too_long_for_a_part_still_splits_at_semicolons(self):
        text = "; ".join(f"clause {i} goes on" for i in range(1000)) + "."
        parts = cut_blocks([("p", text)], 5)
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0][-1][1].endswith(";"))

    def test_split_verse_keeps_its_line_breaks(self):
        verse = "\n".join(f"A line of verse number {i}{'.' if i % 4 == 3 else ','}" for i in range(600))
        parts = cut_blocks([("verse", verse)], 5)
        self.assertGreater(len(parts), 1)
        self.assertIn("\n", parts[1][0][1])
        self.assertEqual("\n".join(b[1] for part in parts for b in part), verse)

    def test_a_lead_in_keeps_the_heading_it_introduces(self):
        story = ("p", "word " * 1400 + "and is entitled:")
        found = [("h2", "I."), story, ("h3", "II. KING BEAN."), ("p", "Once upon a time. " * 300),
                 ("h2", "III."), ("p", "The end of it. " * 400)]
        parts = cut_blocks(found, 10, skip_front=False)
        self.assertEqual(parts[0][-1], ("p", "Once upon a time. " * 300))
        self.assertEqual(parts[1][0], ("h2", "III."))

    def test_more_note_and_page_markers_after_the_stop(self):
        for end in ("the Church.[236:3]", "they govern themselves.”⁠[5]", "in feigned slumbers!’(3)",
                    "at a very early age. {200b}", "of Chaucer. <3>"):
            self.assertTrue(ends_sentence([("p", end)]), end)
        self.assertFalse(ends_sentence([("p", "It is called (3)")]))

    def test_a_lead_in_before_a_big_paragraph_takes_its_first_sentences(self):
        found = [("p", "Some words here. " * 300), ("p", "and the tale is called:"),
                 ("p", "There was once a baker. " * 200)]
        parts = cut_blocks(found, 5, skip_front=False)
        self.assertTrue(all(ends_sentence(p) for p in parts[:-1]), [p[-1][1][-30:] for p in parts])
        self.assertEqual(parts[0][-2], ("p", "and the tale is called:"))
        self.assertEqual(" ".join(b[1] for p in parts for b in p).split(), " ".join(t for _, t in found).split())

    def test_a_closing_index_goes(self):
        found = [("h2", "CHAPTER I."), ("p", "Text. " * 1000), ("h2", "INDEX"), ("p", "Stars, i, 4; and early man, i, 127"),
                 ("p", "Zeus, ii, 3")]
        self.assertEqual(drop_editorial(found), found[:2])
        lettered = found[:3] + [("h3", "B."), ("p", "Bacon, 12"), ("h2", "APPENDIX"), ("p", "The original text.")]
        self.assertEqual(drop_editorial(lettered), found[:2] + lettered[-2:], "what follows the index stays")
        early = [("h2", "INDEX"), ("p", "Abel, 3")] + found[:2]
        self.assertEqual(drop_editorial(early), early, "an index at the start isn't the closing one")

    def test_a_footnote_label_alone_takes_the_next_block(self):
        page = ('<p>Text<a href="#f2">2</a> here.</p><p>On.</p><div><a id="f2"><span>Footnote 2:</span></a></div>'
                '<div>  cleaning</div><div><br></div><div><a href="#fr2">return</a></div>')
        self.assertEqual(blocks(page), [("p", "Text<sup>2</sup> here."), ("note", "Footnote 2: cleaning"), ("p", "On.")])

    def test_the_footnotes_line_over_moved_notes_goes(self):
        page = ('<p>Text<a href="#n1">[1]</a>.</p><p>FOOTNOTES:</p><div class="footnote"><p><a id="n1">[1]</a> A note.</p></div>'
                '<h2>CHAPTER 2.</h2><p>More.</p>')
        self.assertEqual(blocks(page), [("p", "Text[1]."), ("note", "[1] A note."), ("h2", "CHAPTER 2."), ("p", "More.")])


if __name__ == "__main__":
    unittest.main()
