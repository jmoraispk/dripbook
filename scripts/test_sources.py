"""Offline tests for sources.py's scan cleanup: python3 -m unittest discover -s scripts -p 'test_sources.py'"""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "drip"))
import sources  # noqa: E402

PROSE = ("It was a time of great change in the arts and in the ways that people spoke of them, "
         "and the critics of the day were not slow to say so at every turn of the road ")


def word(text, height=30):
    return f'<WORD coords="0,{100 + height},10,100">{text}</WORD>'


def page(*paras):
    """paras: lists of lines; a (lines, height) pair sets the type size."""
    out = []
    for p in paras:
        lines, h = p if isinstance(p, tuple) else (p, 30)
        out.append("<PARAGRAPH>" + "".join("<LINE>" + "".join(word(w, h) for w in ln.split()) + "</LINE>"
                                           for ln in lines) + "</PARAGRAPH>")
    return "<OBJECT>" + "".join(out) + "</OBJECT>"


def book(*pages):
    return ("<DjVuXML><BODY>" + "".join(pages) + "</BODY></DjVuXML>").encode()


class Lines(unittest.TestCase):
    def test_joins_broken_words_and_fixes_spacing(self):
        self.assertEqual(sources.join_lines(["the pro-", "fessional critic ; he said \" heady . \""]),
                         'the professional critic; he said "heady."')

    def test_running_heads(self):
        self.assertEqual(sources.running_head([["Preface ix"], ["text"]]), (1, "ix"))
        self.assertEqual(sources.running_head([["12 THE PHILOSOPHY OF THE BEAUTIFUL"]]), (1, "12"))
        self.assertEqual(sources.running_head([["IX"], ["Music 127"]]), (2, "127"))
        self.assertEqual(sources.running_head([["CHAPTER II"], ["THE NATURE OF BEAUTY"]]), (0, None))


class Blocks(unittest.TestCase):
    def test_a_page_turn_footnote_and_heads(self):
        xml = book(
            page(["CHAPTER I"], ["PROLEGOMENA"], [PROSE * 3, "and so it went on, with no"],
                 (["1 See the earlier volume, p. 12."], 18)),
            page(["2 THE PHILOSOPHY OF THE BEAUTIFUL"], ["end in sight. Then it stopped."], [PROSE * 3 + "."]),
            page(["Digitized by Google"], ["INDEX"], ["Art, 1, 2"]))
        blocks = sources.scan_blocks(sources.scan_pages(xml))
        kinds = [k for k, _ in blocks]
        # the footnote follows its paragraph, which ends on the next page; the index at the end is left out
        self.assertEqual(kinds, ["h", "h", "p", "pg", "note", "p"])
        self.assertTrue(blocks[2][1].endswith("with no end in sight. Then it stopped."), "carried over the page")
        self.assertEqual(blocks[3], ("pg", "2"))
        self.assertNotIn("Google", " ".join(t for _, t in blocks))

    def test_drops_the_index_at_the_end(self):
        xml = book(*[page([PROSE * 4 + "."]) for _ in range(12)], page(["INDEX"], ["Art, 1, 2"]))
        blocks = sources.scan_blocks(sources.scan_pages(xml))
        self.assertNotIn("INDEX", [t for _, t in blocks])

    def test_lessons_use_the_editor_kinds(self):
        self.assertEqual(sources.as_lessons([[("h", "One"), ("p", "x"), ("pg", "3"), ("h3", "y")]]),
                         [[["h2", "One"], ["p", "x"], ["pg", "3"], ["h3", "y"]]])


class Hathi(unittest.TestCase):
    def test_every_word_in_the_title_or_author(self):
        import gzip, tempfile
        rows = [sources.HATHI_FIELDS,
                ["uc1.a", "pd", "1", "", "Dracula / by Bram Stoker.", "London", "1897", "Stoker, Bram, 1847-1912."],
                ["uc1.b", "pdus", "2", "", "The jewel of seven stars", "London", "1903", "Stoker, Bram, 1847-1912."],
                ["uc1.c", "pd", "3", "", "Dracula's guest", "London", "1914", "Stoker, Bram"]]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hathi.tsv.gz"
            with gzip.open(path, "wt", encoding="utf-8") as f:
                f.write("\n".join("\t".join(r) for r in rows) + "\n")
            with unittest.mock.patch.object(sources, "HATHI_INDEX", path), unittest.mock.patch.dict(sources.HATHI, clear=True):
                self.assertEqual([r["id"] for r in sources.search_hathi("stoker dracula")], ["hathi:uc1.a", "hathi:uc1.c"])
                self.assertEqual([r["rights"] for r in sources.search_hathi("jewel")], ["Public domain in the US"])
                self.assertEqual(sources.search_hathi("london"), [], "the publisher isn't searched")


if __name__ == "__main__":
    unittest.main()
