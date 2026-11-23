"""Offline tests for convert.py: python3 -m unittest discover -s converter

They use only the standard library. PDFs are simulated as lists of positioned
characters, and the Claude call is faked. One test reads the real Think Python
PDF when pdfplumber is installed and converter/output/thinkpython2.pdf exists.
"""
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import convert  # noqa: E402

W = 5.0  # advance width of every simulated character


def chars(text, x, base, size=10.0, font="Palatino-Roman"):
    """Positioned characters for one line of text. Spaces are gaps, as in most PDFs."""
    out = []
    for ch in text:
        if ch != " ":
            out.append((ch, x, x + W, base - 0.8 * size, base, size, font))
        x += W
    return out


def runs(*parts):
    """chars() for a line made of differently styled pieces: runs(("int ", "Mono"), ("is a type", "Roman"))."""
    def build(x, base, size=10.0):
        out = []
        for text, font in parts:
            out += chars(text, x, base, size, font)
            x += W * len(text)
        return out
    return build


def page(number, *lines, graphics=(), images=()):
    return {"number": number, "width": 612.0, "height": 792.0, "chars": [c for l in lines for c in l],
            "graphics": list(graphics), "images": list(images)}


LEFT = 72.0
FULL = 90  # characters in a full-width line: 72 + 90*5 = 522


def para(lines, start, spacing=12.0):
    """Lines of a paragraph, the last one short."""
    return [chars(t, LEFT, start + i * spacing) for i, t in enumerate(lines)]


def filler(n, word="text"):
    return " ".join([word] * n)


def full_line(start, end=""):
    """A line that reaches the right margin: start, filler words, then end."""
    middle = filler(40)[:FULL - len(start) - len(end) - 2]
    return f"{start} {middle} {end}".strip()


def sample_book():
    """Two pages: a header, a heading, paragraphs, code, a list, a definition list, and a page break mid-paragraph."""
    p1 = page(
        1,
        chars("1 Chapter One. Basics", LEFT, 60, font="Palatino-Bold"),
        chars("Chapter 1", LEFT, 120, size=20, font="Palatino-Bold"),
        chars("Basics", LEFT, 170, size=24, font="Palatino-Bold"),
        *para([full_line("The first paragraph is", "about state-"), "ments and com-"], 220),
        *para([full_line("A second paragraph introduces some code"), "like this:"], 262),
        chars("def greet(name):", LEFT, 292, font="Courier"),
        chars("    print('hi', name)", LEFT, 304, font="Courier"),
        chars("greet('Ada')", LEFT, 328, font="Courier"),
        *para(["Some points:"], 352),
        chars("• first point", LEFT, 376),
        chars("• second point that runs long enough to wrap onto", LEFT, 388),
        chars("another line", LEFT + 12, 400),
        runs(("term:", "Palatino-Bold"), (" a word with a meaning.", "Palatino-Roman"))(LEFT, 424),
        runs(("other term:", "Palatino-Bold"), (" " + full_line("another meaning")[:FULL - 12], "Palatino-Roman"))(LEFT, 448),
        chars("to wrap.", LEFT + 20, 460),
        chars(full_line("This paragraph", "does not end on this page and"), LEFT, 700),
    )
    p2 = page(
        2,
        chars("Chapter One. Basics 2", LEFT, 60, font="Palatino-Bold"),
        chars("carries on here. The end.", LEFT, 100),
        chars("1.1 More", LEFT, 140, size=14, font="Palatino-Bold"),
        runs(("Use ", "Palatino-Roman"), ("print", "Courier"), (" to show a value.", "Palatino-Roman"))(LEFT, 212),
    )
    return {"title": "", "author": "", "page_count": 2, "pages": [p1, p2]}


class FontTests(unittest.TestCase):
    def test_styles(self):
        self.assertEqual(convert.font_style("ABCDEF+SFTT1000"), "code")
        self.assertEqual(convert.font_style("Courier-Bold"), "code")
        self.assertEqual(convert.font_style("URWPalladioL-Bold"), "b")
        self.assertEqual(convert.font_style("URWPalladioL-Ital"), "i")
        self.assertEqual(convert.font_style("Helvetica-BoldOblique"), "bi")
        self.assertEqual(convert.font_style("URWPalladioL-Roma"), "")


class LineTests(unittest.TestCase):
    def test_spaces_rebuilt_from_gaps(self):
        [line] = convert.lines_from_chars(chars("hello big world", 72, 100))
        self.assertEqual(line["text"], "hello big world")

    def test_monospace_spacing_is_exact(self):
        [line] = convert.lines_from_chars(chars("x  =    1", 72, 100, font="Courier"))
        self.assertEqual(line["text"], "x  =    1")
        self.assertEqual(line["mono"], 1.0)

    def test_styles_become_runs(self):
        build = runs(("call ", "Roman"), ("print", "Courier"), (" now", "Roman"))
        [line] = convert.lines_from_chars(build(72, 100))
        self.assertEqual(line["runs"], [["call ", ""], ["print", "code"], [" now", ""]])

    def test_ligatures_and_soft_hyphens(self):
        [line] = convert.lines_from_chars(chars("ﬁne", 72, 100) + [("­", 90, 91, 92, 100, 10, "Roman")])
        self.assertEqual(line["text"], "fine")

    def test_superscript(self):
        cs = chars("x", 72, 100) + [("2", 77, 80, 94, 96, 7, "Roman")]
        [line] = convert.lines_from_chars(cs)
        self.assertEqual(line["runs"], [["x", ""], ["2", "sup"]])


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.doc = convert.parse(sample_book())
        self.blocks = self.doc["blocks"]
        self.by_text = {b.get("text", "")[:12]: b for b in self.blocks}

    def test_running_header_removed(self):
        self.assertFalse(any("Chapter One. Basics" in b.get("text", "") for b in self.blocks))

    def test_chapter_heading_merged(self):
        h = self.blocks[0]
        self.assertEqual((h["type"], h["level"], h["text"]), ("heading", 1, "Chapter 1\nBasics"))
        self.assertEqual(h["id"], "p1.1")

    def test_hyphenation(self):
        p = self.blocks[1]
        self.assertIn("about statements and com-", p["text"])
        self.assertTrue(p["text"].endswith("and com-"), "a hyphen at the end of a paragraph stays")

    def test_code_block_keeps_indentation_and_blank_lines(self):
        code = next(b for b in self.blocks if b["type"] == "code")
        self.assertEqual(code["text"], "def greet(name):\n    print('hi', name)\n\ngreet('Ada')")

    def test_list_items(self):
        items = [b for b in self.blocks if b["type"] == "list_item"]
        self.assertEqual([i["marker"] for i in items], ["•", "•"])
        self.assertEqual(items[1]["text"], "second point that runs long enough to wrap onto another line")

    def test_definition_entries_are_separate_paragraphs(self):
        texts = [b["text"] for b in self.blocks if b["type"] == "paragraph"]
        self.assertIn("term: a word with a meaning.", texts)
        self.assertTrue(any(t.startswith("other term: another meaning") and t.endswith(" to wrap.") for t in texts))

    def test_paragraph_continues_across_pages(self):
        texts = [b["text"] for b in self.blocks]
        self.assertTrue(any(t.endswith("does not end on this page and carries on here. The end.") for t in texts))

    def test_section_heading_level(self):
        h = [b for b in self.blocks if b["type"] == "heading"]
        self.assertEqual([(b["text"], b["level"]) for b in h], [("Chapter 1\nBasics", 1), ("1.1 More", 2)])

    def test_inline_code_kept(self):
        b = next(b for b in self.blocks if b["text"].startswith("Use print to show"))
        self.assertIn(["print", "code"], b["runs"])


class FigureTests(unittest.TestCase):
    def test_drawing_with_caption_becomes_figure(self):
        p = page(5,
                 *para([full_line("Before the figure"), "ends here."], 100),
                 chars("label", 200, 220), chars("arrow target", 300, 250),
                 chars("Figure 3.1: A diagram.", 200, 300),
                 *para(["After the figure."], 330),
                 graphics=[(180, 200, 420, 280, "curve"), (180, 200, 420, 280, "rect")])
        doc = convert.parse({"title": "", "author": "", "page_count": 9, "pages": [p]})
        types = [b["type"] for b in doc["blocks"]]
        self.assertEqual(types, ["paragraph", "figure", "paragraph"])
        self.assertEqual(doc["blocks"][1]["caption"], "Figure 3.1: A diagram.")
        self.assertFalse(any("label" in b.get("text", "") for b in doc["blocks"]))

    def test_box_around_code_keeps_its_text(self):
        p = page(5,
                 *para(["Code in a rounded box:"], 100),
                 chars("x = 1", 90, 130, font="Courier"), chars("y = 2", 90, 142, font="Courier"),
                 chars("print(x + y)", 90, 154, font="Courier"),
                 graphics=[(80, 118, 500, 162, "curve")])
        doc = convert.parse({"title": "", "author": "", "page_count": 9, "pages": [p]})
        code = [b for b in doc["blocks"] if b["type"] == "code"]
        self.assertEqual(code[0]["text"], "x = 1\ny = 2\nprint(x + y)")

    def test_line_drawing_is_a_figure(self):
        lines = [(100 + i * 10, 200, 110 + i * 10, 260, "line") for i in range(20)]
        p = page(5, *para(["Text."], 100), chars("Figure 1.2: Lines.", 150, 280), graphics=lines)
        doc = convert.parse({"title": "", "author": "", "page_count": 9, "pages": [p]})
        self.assertEqual(doc["blocks"][-1]["type"], "figure")
        self.assertEqual(doc["blocks"][-1]["caption"], "Figure 1.2: Lines.")

    def test_diagram_with_aligned_labels_is_a_figure(self):
        # a stack diagram: frames in a column with labels that line up, and arrows inside
        frames = [(150, 200 + i * 30, 300, 226 + i * 30, "rect") for i in range(3)]
        arrows = [(180, 208 + i * 30, 200, 212 + i * 30, "curve") for i in range(3)]
        labels = [chars(f"n {i}", 160, 214 + i * 30) for i in range(3)]
        title = chars("list", 150, 196, size=9)
        p = page(5, *para(["Text."], 100), *labels, title, graphics=frames + arrows)
        doc = convert.parse({"title": "", "author": "", "page_count": 9, "pages": [p]})
        fig = doc["blocks"][-1]
        self.assertEqual(fig["type"], "figure")
        self.assertFalse(any(b.get("text", "").startswith(("n ", "list")) for b in doc["blocks"]))
        self.assertLessEqual(fig["box"][1], 196 - 0.8 * 9, "the label above the frames is inside the crop")

    def test_pieces_side_by_side_are_one_figure(self):
        shapes = [(100, 200, 160, 260, "curve"), (220, 205, 280, 255, "curve"), (340, 200, 400, 262, "curve")]
        p = page(5, *para(["Text."], 100), graphics=shapes)
        doc = convert.parse({"title": "", "author": "", "page_count": 9, "pages": [p]})
        figs = [b for b in doc["blocks"] if b["type"] == "figure"]
        self.assertEqual(len(figs), 1)
        self.assertEqual(figs[0]["box"], [96.0, 196.0, 404.0, 266.0])

    def test_caption_without_figure_is_flagged(self):
        p = page(5, *para(["Figure 4.1: Turtle flowers."], 100), *para(["Figure 4.1 shows flowers."], 130))
        doc = convert.parse({"title": "", "author": "", "page_count": 9, "pages": [p]})
        self.assertEqual(len(doc["warnings"]), 1)
        self.assertIn("Turtle flowers", doc["warnings"][0])

    def test_image_is_reported(self):
        p = page(5, *para(["Text."], 100), images=[(100, 200, 400, 400)])
        doc = convert.parse({"title": "", "author": "", "page_count": 9, "pages": [p]})
        self.assertEqual(doc["blocks"][-1]["type"], "figure")
        self.assertEqual(doc["blocks"][-1]["kind"], "image")


def make_blocks(spec):
    """Blocks from [(type, words, level)], with ids b1, b2..."""
    out = []
    for i, (kind, words, *level) in enumerate(spec, 1):
        b = {"id": f"p{i}.1", "type": kind, "page": i, "text": filler(words) if words else ""}
        if kind == "heading":
            b["level"] = level[0]
        if kind == "figure":
            b.update(caption="Figure 1", text="")
            del b["text"]
        out.append(b)
    return out


class ChunkTests(unittest.TestCase):
    def test_short_chapters_are_packed_together(self):
        blocks = make_blocks([("heading", 2, 1), ("paragraph", 300), ("heading", 2, 1), ("paragraph", 300),
                              ("heading", 2, 1), ("paragraph", 300)])
        chunks = convert.chunk_blocks(blocks, 700)
        self.assertEqual([len(c) for c in chunks], [4, 2])

    def test_long_chapter_split_at_sections(self):
        blocks = make_blocks([("heading", 2, 1), ("paragraph", 400), ("heading", 2, 2), ("paragraph", 400),
                              ("heading", 2, 2), ("paragraph", 400)])
        chunks = convert.chunk_blocks(blocks, 500)
        self.assertEqual([c[0]["type"] for c in chunks], ["heading"] * 3)
        self.assertEqual(sum(len(c) for c in chunks), len(blocks))

    def test_no_headings_falls_back_to_blocks(self):
        blocks = make_blocks([("paragraph", 300)] * 5)
        chunks = convert.chunk_blocks(blocks, 700)
        self.assertEqual([len(c) for c in chunks], [2, 2, 1])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.blocks = make_blocks([("paragraph", 50), ("heading", 2, 1), ("paragraph", 700), ("code", 30),
                                   ("paragraph", 200), ("heading", 2, 1), ("paragraph", 800), ("figure", 0),
                                   ("paragraph", 100)])

    def plan(self, **changes):
        plan = {"lessons": [
            {"first_block": "p2.1", "last_block": "p5.1", "subject": "One", "lead_in": "", "teaser": "Two next."},
            {"first_block": "p6.1", "last_block": "p9.1", "subject": "Two", "lead_in": "Intro.", "teaser": ""},
        ], "skipped": [{"first_block": "p1.1", "last_block": "p1.1", "reason": "title page"}], "cta_note": ""}
        plan.update(changes)
        return plan

    def test_valid_plan(self):
        self.assertEqual(convert.plan_errors(self.plan(), self.blocks), [])

    def test_gap(self):
        errors = convert.plan_errors(self.plan(skipped=[]), self.blocks)
        self.assertEqual(errors, ["blocks p1.1 to p1.1 are in no lesson or skipped range"])

    def test_trailing_gap(self):
        plan = self.plan()
        plan["lessons"][1]["last_block"] = "p8.1"
        self.assertIn("blocks p9.1 to p9.1 are in no lesson or skipped range", convert.plan_errors(plan, self.blocks))

    def test_overlap(self):
        plan = self.plan()
        plan["lessons"][1]["first_block"] = "p5.1"
        self.assertTrue(any("overlaps" in e for e in convert.plan_errors(plan, self.blocks)))

    def test_unknown_and_reversed_ids(self):
        plan = self.plan()
        plan["lessons"][0]["last_block"] = "p99.1"
        plan["lessons"][1]["first_block"], plan["lessons"][1]["last_block"] = "p9.1", "p6.1"
        errors = convert.plan_errors(plan, self.blocks)
        self.assertTrue(any("unknown block id 'p99.1'" in e for e in errors))
        self.assertTrue(any("comes after" in e for e in errors))

    def test_lessons_out_of_order(self):
        plan = self.plan()
        plan["lessons"].reverse()
        self.assertTrue(any("out of order" in e for e in convert.plan_errors(plan, self.blocks)))

    def test_added_text_is_one_short_line(self):
        plan = self.plan()
        plan["lessons"][0]["teaser"] = "Line one.\nLine two."
        plan["lessons"][1]["subject"] = "x" * 200
        plan["lessons"][1]["lead_in"] = ""
        errors = convert.plan_errors(plan, self.blocks)
        self.assertTrue(any("teaser must be one line" in e for e in errors))
        self.assertTrue(any("subject is 200 characters" in e for e in errors))
        plan = self.plan()
        plan["lessons"][0]["subject"] = " "
        self.assertIn("lesson 1: subject is empty", convert.plan_errors(plan, self.blocks))

    def course(self, plan=None, free=1):
        doc = {"title": "", "author": "", "blocks": self.blocks, "warnings": []}
        settings = {"free_lessons": free, "price": 49, "cta_url": "https://pay.example/x", "title": "A Book",
                    "author": "Ann Author", "fallback_title": "file", "source": {"file": "a.pdf", "pages": [1, 9]}}
        return convert.build_course(plan or self.plan(), doc, settings)

    def test_course_is_built_from_the_pdf_text(self):
        course, errors = self.course()
        self.assertEqual(errors, [])
        one, two = course["lessons"]
        self.assertEqual((one["day"], two["day"]), (1, 2))
        self.assertEqual([b["id"] for b in one["blocks"]], ["p2.1", "p3.1", "p4.1", "p5.1"])
        self.assertEqual(one["words"], 2 + 700 + 30 + 200)
        self.assertEqual(one["minutes"], round(932 / convert.WORDS_PER_MINUTE, 1))
        self.assertTrue(one["free"])
        self.assertFalse(two["free"])
        self.assertEqual([b["id"] for b in two["blocks"] if b["type"] == "figure"], ["p8.1"])
        self.assertEqual(course["skipped"][0]["reason"], "title page")
        self.assertEqual(course["book"], {"title": "A Book", "author": "Ann Author", "slug": "a-book"})

    def test_warnings(self):
        plan = self.plan()
        plan["lessons"][0]["last_block"] = "p6.1"     # ends on a heading
        plan["lessons"][1]["first_block"] = "p7.1"
        course, _ = self.course(plan)
        self.assertTrue(any("ends on a heading" in w for w in course["warnings"]))

    def test_edited_text_in_plan_is_flagged(self):
        plan = self.plan()
        plan["lessons"][0]["blocks"] = [{"text": "invented words"}]
        course, _ = self.course(plan)
        self.assertTrue(any("differs from the PDF" in w for w in course["warnings"]))


def fake_response(stop_reason, data):
    text = data if isinstance(data, str) else json.dumps(data)
    return NS(stop_reason=stop_reason, content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
              usage=NS(input_tokens=1000, output_tokens=100))


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.beta = NS(messages=self)

    def stream(self, **params):
        self.calls.append({**params, "messages": list(params["messages"])})
        return contextlib.nullcontext(NS(get_final_message=lambda r=self.responses.pop(0): r))


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.blocks = make_blocks([("heading", 2, 1), ("paragraph", 700), ("paragraph", 300)])
        self.args = NS(model="claude-opus-5-5", effort="high", chunk_words=12000, dry_run=False)
        self.good = {"book_title": "T", "book_author": "A", "cta_note": "", "skipped": [], "lessons": [
            {"first_block": "p1.1", "last_block": "p3.1", "subject": "S", "lead_in": "", "teaser": ""}]}

    def test_request_shape(self):
        client = FakeClient([fake_response("end_turn", self.good)])
        data, _ = convert.plan_chunk(client, self.args, self.blocks, "prompt")
        self.assertEqual(data, self.good)
        call = client.calls[0]
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertEqual(call["thinking"], {"type": "adaptive"})
        self.assertEqual(call["output_config"]["format"]["schema"], convert.PLAN_SCHEMA)
        self.assertEqual(call["fallbacks"], "default")

    def test_invalid_plan_is_sent_back_with_the_errors(self):
        bad = dict(self.good, lessons=[dict(self.good["lessons"][0], last_block="p2.1")])
        client = FakeClient([fake_response("end_turn", bad), fake_response("end_turn", "not json"),
                             fake_response("end_turn", self.good)])
        with contextlib.redirect_stdout(io.StringIO()):
            data, _ = convert.plan_chunk(client, self.args, self.blocks, "prompt")
        self.assertEqual(data, self.good)
        retry = client.calls[1]["messages"]
        self.assertEqual([m["role"] for m in retry], ["user", "assistant", "user"])
        self.assertIn("p3.1 to p3.1 are in no lesson", retry[2]["content"])

    def test_gives_up_after_three_tries(self):
        client = FakeClient([fake_response("end_turn", "{}")] * 3)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(convert.PlanError):
            convert.plan_chunk(client, self.args, self.blocks, "prompt")

    def test_refusal(self):
        client = FakeClient([fake_response("refusal", "")])
        with self.assertRaises(convert.PlanError):
            convert.plan_chunk(client, self.args, self.blocks, "prompt")

    def test_prompt_lists_blocks_and_next_part(self):
        prompt = convert.chunk_prompt(self.blocks, 1, 2, {"title": "T", "author": "A"}, [],
                                      [{"type": "paragraph", "text": "What comes next"}])
        self.assertIn("[p1.1] heading level 1, 2 words", prompt)
        self.assertIn("What comes next", prompt)
        self.assertIn("Plan the lessons for blocks p1.1 to p3.1.", prompt)

    def test_plan_with_claude_joins_parts(self):
        blocks = make_blocks([("heading", 2, 1), ("paragraph", 700), ("heading", 2, 1), ("paragraph", 700)])
        part1 = dict(self.good, lessons=[dict(self.good["lessons"][0], last_block="p2.1")], cta_note="More soon.")
        part2 = dict(self.good, lessons=[dict(self.good["lessons"][0], first_block="p3.1", last_block="p4.1")])
        client = FakeClient([fake_response("end_turn", part1), fake_response("end_turn", part2)])
        args = NS(**{**vars(self.args), "chunk_words": 800})
        with mock.patch.dict(sys.modules, {"anthropic": NS(Anthropic=lambda **kw: client)}), \
                contextlib.redirect_stdout(io.StringIO()):
            plan = convert.plan_with_claude({"blocks": blocks}, args, {"title": "", "author": ""})
        self.assertEqual([l["first_block"] for l in plan["lessons"]], ["p1.1", "p3.1"])
        self.assertEqual(plan["cta_note"], "More soon.")
        self.assertEqual(plan["book"]["title"], "T")
        self.assertIn("The last one is \"S\"", client.calls[1]["messages"][0]["content"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        blocks = [
            {"id": "p1.1", "type": "heading", "page": 1, "level": 1, "text": "Chapter 1\nStart"},
            {"id": "p1.2", "type": "paragraph", "page": 1, "text": "Use print <here> & now.",
             "runs": [["Use ", ""], ["print", "code"], [" <here> & now.", ""]]},
            {"id": "p1.3", "type": "code", "page": 1, "text": "if x < 1:\n    print('a & b')\n\ny = 2"},
            {"id": "p1.4", "type": "list_item", "page": 1, "marker": "1.", "level": 1, "text": "One"},
            {"id": "p1.5", "type": "list_item", "page": 1, "marker": "2.", "level": 2, "text": "Two"},
            {"id": "p2.1", "type": "figure", "page": 2, "kind": "drawing", "caption": "Figure 1.1: Boxes."},
            {"id": "p2.2", "type": "paragraph", "page": 2, "text": "Second lesson."},
        ]
        doc = {"title": "", "author": "", "blocks": blocks, "warnings": []}
        plan = {"lessons": [
            {"first_block": "p1.1", "last_block": "p2.1", "subject": "First & best", "lead_in": "Read on.",
             "teaser": "More <soon>."},
            {"first_block": "p2.2", "last_block": "p2.2", "subject": "Second", "lead_in": "", "teaser": ""},
        ], "cta_note": "Then lots more."}
        settings = {"free_lessons": 1, "price": 49, "cta_url": "https://pay.example/c?a=1&b=2", "title": "Book",
                    "author": "Ann", "fallback_title": "book", "source": {"file": "b.pdf", "pages": [1, 2]}}
        self.course, errors = convert.build_course(plan, doc, settings)
        self.assertEqual(errors, [])

    def test_email_html(self):
        html = convert.email_html(self.course["lessons"][0], self.course)
        self.assertIn("<pre", html)
        self.assertIn("if x &lt; 1:\n    print(&#x27;a &amp; b&#x27;)\n\ny = 2</pre>", html)
        self.assertIn("white-space:pre", html)
        self.assertIn("overflow-x:auto", html)
        self.assertIn("Chapter 1<br>Start", html)
        self.assertIn(">print</code> &lt;here&gt; &amp; now.", html)
        self.assertIn("First &amp; best", html)
        self.assertIn("<strong>Tomorrow:</strong> More &lt;soon&gt;.", html)
        self.assertIn("Figure not shown.", html)
        self.assertIn("Continue the course for $49", html)
        self.assertIn('href="https://pay.example/c?a=1&amp;b=2"', html)
        self.assertIn("This was the last free lesson. Then lots more.", html)
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertNotIn("<script", html)

    def test_cta_only_on_last_free_lesson(self):
        self.course["settings"]["free_lessons"] = 2
        first = convert.email_html(self.course["lessons"][0], self.course)
        self.assertNotIn("Continue the course", first)

    def test_plain_text(self):
        text = convert.email_text(self.course["lessons"][0], self.course)
        self.assertIn("Use print <here> & now.", text)
        self.assertIn("    if x < 1:\n        print('a & b')\n\n    y = 2", text)
        self.assertIn("1. One\n  2. Two", text)
        self.assertIn("Tomorrow: More <soon>.", text)
        self.assertIn("Continue the course for $49: https://pay.example/c?a=1&b=2", text)
        self.assertIn("[Figure not shown. Figure 1.1: Boxes. It is on page 2 of the book.]", text)

    def test_preview_page(self):
        with tempfile.TemporaryDirectory() as tmp:  # a figure that can't be cropped is listed on the page
            convert.render_figures(self.course, "b.pdf", Path(tmp), crop_image=mock.Mock(side_effect=OSError("bad")))
        email = convert.email_html(self.course["lessons"][0], self.course)
        page = convert.preview_html(self.course, {1: email})
        self.assertEqual(page.count("<iframe"), 1)
        self.assertIn('role="tab" id="tab-1"', page)
        self.assertNotIn("function fit", page)
        self.assertIn("row-locked", page)
        self.assertIn("Second", page)
        self.assertIn("Figure 1.1: Boxes. (page 2)", page)
        self.assertIn("--ribbon:#F4B425", page)


def book(*spec):
    """Blocks from ("h1", "Title") / ("h2", "Title") / ("p", words) / ("code", "text") / ("li", words)."""
    out, page = [], 1
    for i, (kind, value) in enumerate(spec, 1):
        b = {"id": f"p{page}.{i}", "page": page}
        if kind in ("h1", "h2"):
            b.update(type="heading", level=int(kind[1]), text=value)
        elif kind == "code":
            b.update(type="code", text=value)
        elif kind == "li":
            b.update(type="list_item", marker="•", level=1, text=filler(value))
        elif kind == "toc":
            b.update(type="paragraph", text=value)
        else:
            b.update(type="paragraph", text=filler(value) + (":" if kind == "p:" else ""))
        out.append(b)
    return out


class SplitTests(unittest.TestCase):
    def lessons(self, blocks, **kw):
        plan = convert.split_by_headings(blocks, **kw)
        self.assertEqual(convert.plan_errors(plan, blocks), [])
        ids = {b["id"]: i for i, b in enumerate(blocks)}
        return plan, [[b["id"] for b in blocks[ids[l["first_block"]]:ids[l["last_block"]] + 1]] for l in plan["lessons"]]

    def test_short_sections_are_joined_and_chapters_start_lessons(self):
        blocks = book(("h1", "Chapter 1"), ("p", 300), ("h2", "1.1 A"), ("p", 300), ("h2", "1.2 B"), ("p", 400),
                      ("h2", "1.3 C"), ("p", 500), ("h1", "Chapter 2"), ("p", 600))
        plan, groups = self.lessons(blocks)
        self.assertEqual([l["subject"] for l in plan["lessons"]], ["Chapter 1", "1.3 C", "Chapter 2"])
        self.assertEqual(plan["lessons"][0]["teaser"], "1.3 C")
        self.assertEqual(plan["lessons"][-1]["teaser"], "")

    def test_long_section_cut_between_paragraphs_not_before_code(self):
        blocks = book(("h2", "Long"), ("p", 500), ("p:", 450), ("code", "x = 1"), ("p", 200), ("p", 500), ("p", 500))
        plan, groups = self.lessons(blocks)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0][-1], "p1.5", "the cut comes after the code and its explanation")
        self.assertEqual(plan["lessons"][1]["subject"], "Long (continued)")

    def test_lists_are_not_cut(self):
        blocks = book(("h2", "List"), ("p", 600), *[("li", 100)] * 8, ("p", 500))
        _, groups = self.lessons(blocks)
        for g in groups[:-1]:
            self.assertNotEqual(g[-1], "p1.3")
        self.assertTrue(all(not (g[-1].startswith("p1.") and int(g[-1].split(".")[1]) in range(3, 10)) for g in groups))

    def test_short_end_of_chapter_joins_previous_lesson(self):
        blocks = book(("h1", "One"), ("p", 900), ("h2", "Exercises"), ("p", 150), ("h1", "Two"), ("p", 900))
        plan, groups = self.lessons(blocks)
        self.assertEqual([l["subject"] for l in plan["lessons"]], ["One", "Two"])

    def test_contents_and_index_are_skipped(self):
        toc = "1.1 What is a program? . . . . . . . . 1"
        blocks = book(("h1", "Contents"), *[("toc", toc)] * 6, ("h1", "Chapter 1"), ("p", 700), ("h1", "Index"),
                      ("p", 300))
        for b in blocks[:7]:
            b["page"] = 9
        plan, groups = self.lessons(blocks)
        self.assertEqual([s["reason"] for s in plan["skipped"]], ["table of contents", "index"])
        self.assertEqual([l["subject"] for l in plan["lessons"]], ["Chapter 1"])


class UrlTests(unittest.TestCase):
    def test_spaced_urls_are_rejoined(self):
        fix = lambda t: convert.SPACED_URL.sub(lambda m: "".join(m.group(0).split()), t)  # noqa: E731
        self.assertEqual(fix("from https: // thinkpython. com/ code/ polygon. py ."),
                         "from https://thinkpython.com/code/polygon.py .")
        self.assertEqual(fix("See http: // en. wikipedia. org/ wiki/ Koch_ snowflake for more"),
                         "See http://en.wikipedia.org/wiki/Koch_snowflake for more")
        self.assertEqual(fix("Go to https://example.com. Then read on."), "Go to https://example.com. Then read on.")


class FakeImage:
    width, height = 600, 150

    def __init__(self):
        self.saved = []

    def save(self, path, **kw):
        Path(path).write_bytes(b"png")
        self.saved.append(path)


class FigureRenderTests(unittest.TestCase):
    def course(self, base=""):
        blocks = [
            {"id": "p1.1", "type": "paragraph", "page": 1, "text": "See the figure."},
            {"id": "p1.2", "type": "figure", "page": 1, "kind": "drawing", "caption": "Figure 1.1: Boxes & arrows.",
             "box": [100.0, 200.0, 400.0, 275.0]},
            {"id": "p2.1", "type": "figure", "page": 2, "kind": "image", "caption": "", "box": None},
        ]
        doc = {"title": "", "author": "", "blocks": blocks, "warnings": []}
        plan = {"lessons": [{"first_block": "p1.1", "last_block": "p2.1", "subject": "S", "lead_in": "", "teaser": ""}]}
        settings = {"free_lessons": 1, "price": 49, "cta_url": "#", "image_base_url": base, "title": "B", "author": "",
                    "fallback_title": "b", "source": {}}
        course, errors = convert.build_course(plan, doc, settings)
        self.assertEqual(errors, [])
        return course

    def test_figures_saved_and_shown(self):
        course = self.course()
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            convert.render_figures(course, "book.pdf", Path(tmp),
                                   crop_image=lambda n, box: calls.append((n, box)) or FakeImage())
            self.assertTrue((Path(tmp) / "figures" / "p1-2.png").exists())
        self.assertEqual(calls, [(1, [100.0, 200.0, 400.0, 275.0])])
        fig = course["lessons"][0]["blocks"][1]
        self.assertEqual(fig["image"], {"file": "figures/p1-2.png", "width": 400, "height": 100})
        self.assertEqual(course["figures"][0]["file"], "figures/p1-2.png")
        self.assertEqual(len(course["figures_not_carried_over"]), 1)
        html = convert.email_html(course["lessons"][0], course)
        self.assertIn('<img src="figures/p1-2.png" width="400" alt="Figure 1.1: Boxes &amp; arrows."', html)
        self.assertIn("Figure not shown.", html)  # the one without a position
        text = convert.email_text(course["lessons"][0], course)
        self.assertIn("[Figure 1.1: Boxes & arrows. The image is in the HTML version of this email.]", text)

    def test_public_image_urls(self):
        course = self.course(base="https://blob.example.com/think-python/")
        with tempfile.TemporaryDirectory() as tmp:
            convert.render_figures(course, "book.pdf", Path(tmp), crop_image=lambda n, box: FakeImage())
        html = convert.email_html(course["lessons"][0], course)
        self.assertIn('src="https://blob.example.com/think-python/figures/p1-2.png"', html)
        text = convert.email_text(course["lessons"][0], course)
        self.assertIn("[Figure 1.1: Boxes & arrows. https://blob.example.com/think-python/figures/p1-2.png]", text)


class MainTests(unittest.TestCase):
    def render_saved_settings(self, settings, *overrides):
        raw = sample_book()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pdf = tmp / "book.pdf"
            pdf.write_bytes(b"%PDF")
            ids = [b["id"] for b in convert.parse(raw)["blocks"]]
            plan = {"settings": settings, "lessons": [
                {"first_block": ids[0], "last_block": ids[-1], "subject": "One"}]}
            plan_path = tmp / "plan.json"
            plan_path.write_text(json.dumps(plan))
            out = tmp / "out"
            with mock.patch.object(convert, "read_pdf", return_value=raw), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(convert.main([str(pdf), "--plan", str(plan_path), "--out", str(out), *overrides]), 0)
            return json.loads((out / "lessons.json").read_text()), (out / "day-01.html").read_text()

    def test_render_preserves_saved_payment_and_image_settings(self):
        for price in (19, 0):
            with self.subTest(price=price):
                course, page = self.render_saved_settings({
                    "price": price, "cta_url": "https://checkout.example/course",
                    "image_base_url": "https://images.example/course/"})
                self.assertEqual(course["settings"]["price"], price)
                self.assertEqual(course["settings"]["cta_url"], "https://checkout.example/course")
                self.assertEqual(course["settings"]["image_base_url"], "https://images.example/course/")
                self.assertIn('href="https://checkout.example/course"', page)

    def test_cli_settings_override_saved_settings_including_zero_and_empty_urls(self):
        course, page = self.render_saved_settings(
            {"price": 19, "cta_url": "https://old.example/pay", "image_base_url": "https://old.example/images/"},
            "--price", "0", "--cta-url", "", "--image-base-url", "")
        self.assertEqual(course["settings"]["price"], 0)
        self.assertEqual(course["settings"]["cta_url"], "")
        self.assertEqual(course["settings"]["image_base_url"], "")

    def test_render_without_saved_settings_uses_original_defaults(self):
        course, page = self.render_saved_settings({})
        self.assertEqual(course["settings"], {
            "free_lessons": 1, "price": 49, "cta_url": "{{CHECKOUT_URL}}", "image_base_url": ""})

    def test_render_from_plan_without_api(self):
        raw = sample_book()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pdf = tmp / "book.pdf"
            pdf.write_bytes(b"%PDF-1.7 fake")
            doc = convert.parse(raw)
            ids = [b["id"] for b in doc["blocks"]]
            plan = {"book": {"title": "Sample Book", "author": "Ann"}, "lessons": [
                {"first_block": ids[0], "last_block": ids[5], "subject": "One", "lead_in": "", "teaser": "Two."},
                {"first_block": ids[6], "last_block": ids[-1], "subject": "Two", "lead_in": "", "teaser": ""}]}
            (tmp / "plan.json").write_text(json.dumps(plan))
            out = tmp / "out"
            with mock.patch.object(convert, "read_pdf", return_value=raw), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = convert.main([str(pdf), "--plan", str(tmp / "plan.json"), "--out", str(out), "--free", "1"])
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["day-01.html", "day-01.txt", "day-02.html", "day-02.txt", "lessons.json", "preview.html"])
            course = json.loads((out / "lessons.json").read_text())
            self.assertEqual(course["book"]["slug"], "sample-book")
            self.assertEqual(course["planned_by"], "hand")
            self.assertEqual(len(course["lessons"]), 2)

    def test_default_splits_by_headings_without_claude(self):
        raw = sample_book()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "book.pdf").write_bytes(b"%PDF")
            with mock.patch.object(convert, "read_pdf", return_value=raw), \
                    mock.patch.dict(sys.modules, {"anthropic": None}), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = convert.main([str(tmp / "book.pdf"), "--out", str(tmp / "o"), "--title", "Sample"])
            self.assertEqual(code, 0, stdout.getvalue())
            course = json.loads((tmp / "o" / "lessons.json").read_text())
            self.assertEqual(course["planned_by"], "headings")
            self.assertEqual(course["lessons"][0]["subject"], "Chapter 1: Basics")

    def test_bad_plan_fails(self):
        raw = sample_book()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "book.pdf").write_bytes(b"%PDF")
            (tmp / "plan.json").write_text(json.dumps({"lessons": [
                {"first_block": "p1.1", "last_block": "p1.2", "subject": "One"}]}))
            with mock.patch.object(convert, "read_pdf", return_value=raw), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = convert.main([str(tmp / "book.pdf"), "--plan", str(tmp / "plan.json"), "--out", str(tmp / "o")])
            self.assertEqual(code, 1)
            self.assertIn("are in no lesson or skipped range", err.getvalue())
            self.assertFalse((tmp / "o").exists())

    def test_parse_pages(self):
        self.assertEqual(convert.parse_pages("3-5,9"), [3, 4, 5, 9])
        self.assertEqual(convert.parse_pages("7"), [7])
        with self.assertRaises(Exception):
            convert.parse_pages("a-b")


def node_command():
    """A way to run Node.js: node itself, or uv's nodejs-wheel package. None if neither works."""
    if shutil.which("node"):
        return ["node"]
    if shutil.which("uvx"):
        probe = subprocess.run(["uvx", "-q", "--from", "nodejs-wheel", "node", "--version"], capture_output=True)
        if probe.returncode == 0:
            return ["uvx", "-q", "--from", "nodejs-wheel", "node"]
    return None


class EditorCoreTests(unittest.TestCase):
    """The cut editor's logic (site/cuts-core.js) has its own tests in test_editor_core.mjs."""

    def test_editor_core(self):
        node = node_command()
        if not node:
            self.skipTest("needs Node.js, or uv to fetch it")
        result = subprocess.run(node + ["--test", str(HERE / "test_editor_core.mjs")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_plan_settings_choose_free_lessons(self):
        raw = sample_book()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "book.pdf").write_bytes(b"%PDF")
            ids = [b["id"] for b in convert.parse(raw)["blocks"]]
            plan = {"settings": {"free_lessons": 2}, "lessons": [
                {"first_block": ids[0], "last_block": ids[3], "subject": "One"},
                {"first_block": ids[4], "last_block": ids[7], "subject": "Two"},
                {"first_block": ids[8], "last_block": ids[-1], "subject": "Three"}]}
            (tmp / "plan.json").write_text(json.dumps(plan))
            with mock.patch.object(convert, "read_pdf", return_value=raw), contextlib.redirect_stdout(io.StringIO()):
                convert.main([str(tmp / "book.pdf"), "--plan", str(tmp / "plan.json"), "--out", str(tmp / "o")])
            course = json.loads((tmp / "o" / "lessons.json").read_text())
            self.assertEqual(course["settings"]["free_lessons"], 2)
            self.assertEqual(len(course["all_blocks"]), len(ids))
            self.assertTrue(all("top" in b and "bottom" in b for b in course["all_blocks"]))


class CutsTests(unittest.TestCase):
    """cuts.json, which the cut editor (site/cuts.html) saves for a bare PDF, names positions on pages instead of block ids."""

    def blocks(self):
        spec = [(1, 100, "heading", "Contents"), (1, 200, "paragraph", "1 Start . . . 3"),
                (3, 90, "heading", "Chapter 1\nStart"), (3, 200, "paragraph", "First words of the chapter"),
                (3, 400, "paragraph", "A later paragraph"), (4, 80, "heading", "1.1 More"),
                (4, 300, "paragraph", "Text under the section")]
        return [{"id": f"p{page}.{i}", "page": page, "top": top, "type": kind, "text": text}
                for i, (page, top, kind, text) in enumerate(spec, 1)]

    def test_positions_become_block_ranges(self):
        blocks = self.blocks()
        cuts = {"settings": {"free_lessons": 1}, "segments": [
            {"kind": "skip", "page": 1, "y": 0, "reason": "Contents"},
            {"kind": "lesson", "page": 3, "y": 86, "subject": "Chapter 1: Start", "teaser": "1.1 More"},
            {"kind": "lesson", "page": 3, "y": 380, "subject": ""},
            {"kind": "lesson", "page": 4, "y": 120, "first_words": "1.1 More", "subject": "1.1 More"}]}
        plan, warnings = convert.plan_from_cuts(cuts, blocks)
        self.assertEqual(warnings, [])
        self.assertEqual(convert.plan_errors(plan, blocks), [])
        self.assertEqual(plan["skipped"], [{"first_block": "p1.1", "last_block": "p1.2", "reason": "Contents"}])
        self.assertEqual([(l["first_block"], l["last_block"], l["subject"]) for l in plan["lessons"]],
                         [("p3.3", "p3.4", "Chapter 1: Start"), ("p3.5", "p3.5", "Chapter 1: Start (continued)"),
                          ("p4.6", "p4.7", "1.1 More")])
        self.assertEqual(plan["settings"], {"free_lessons": 1})

    def test_cuts_that_land_together_are_dropped(self):
        blocks = self.blocks()
        cuts = {"segments": [{"kind": "lesson", "page": 1, "y": 0, "subject": "A"},
                             {"kind": "lesson", "page": 3, "y": 300, "subject": "B"},
                             {"kind": "lesson", "page": 3, "y": 310, "subject": "C"}]}
        plan, warnings = convert.plan_from_cuts(cuts, blocks)
        self.assertEqual([l["subject"] for l in plan["lessons"]], ["A", "B"])
        self.assertEqual(len(warnings), 1)

    def test_main_renders_cuts(self):
        raw = sample_book()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "book.pdf").write_bytes(b"%PDF")
            blocks = convert.parse(raw)["blocks"]
            cuts = {"settings": {"free_lessons": 1}, "segments": [
                {"kind": "lesson", "page": blocks[0]["page"], "y": 0, "subject": "One"},
                {"kind": "lesson", "page": blocks[5]["page"], "y": blocks[5]["top"] - 2, "subject": "Two"}]}
            (tmp / "cuts.json").write_text(json.dumps(cuts))
            with mock.patch.object(convert, "read_pdf", return_value=raw), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(convert.main([str(tmp / "book.pdf"), "--cuts", str(tmp / "cuts.json"),
                                               "--out", str(tmp / "o")]), 0)
            course = json.loads((tmp / "o" / "lessons.json").read_text())
            self.assertEqual([l["subject"] for l in course["lessons"]], ["One", "Two"])
            self.assertEqual(course["lessons"][1]["first_block"], blocks[5]["id"])
            self.assertEqual(course["planned_by"], "cuts.json from the cut editor")


THINK_PYTHON = HERE / "output" / "thinkpython2.pdf"
PLAN = HERE / "examples" / "think-python.plan.json"


def have_pdfplumber():
    try:
        import pdfplumber  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(THINK_PYTHON.exists() and have_pdfplumber(),
                     "needs pdfplumber and converter/output/thinkpython2.pdf")
class ThinkPythonTests(unittest.TestCase):
    def test_hand_plan_matches_pdf(self):
        doc = convert.extract(THINK_PYTHON, convert.parse_pages("23-38"))
        plan = json.loads(PLAN.read_text())
        self.assertEqual(convert.plan_errors(plan, doc["blocks"]), [])
        code = {b["id"]: b["text"] for b in doc["blocks"] if b["type"] == "code"}
        self.assertIn("False      class      finally    is         return", "\n".join(code.values()))

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "needs pymupdf")
    def test_engines_agree_on_the_sample(self):
        pages = convert.parse_pages("23-38")
        a = convert.extract(THINK_PYTHON, pages, "pdfplumber")
        b = convert.extract(THINK_PYTHON, pages, "pymupdf")
        plan_a = convert.split_by_headings(a["blocks"])
        plan_b = convert.split_by_headings(b["blocks"])
        self.assertEqual([l["subject"] for l in plan_a["lessons"]], [l["subject"] for l in plan_b["lessons"]])
        code = lambda d: [x["text"] for x in d["blocks"] if x["type"] == "code"]  # noqa: E731
        self.assertEqual(code(a), code(b))

    def test_every_captioned_figure_is_found(self):
        doc = convert.extract(THINK_PYTHON)
        captions = [b["caption"] for b in doc["blocks"] if b["type"] == "figure" and b["caption"]]
        self.assertEqual(len(captions), 26)
        self.assertFalse([w for w in doc["warnings"] if "caption" in w])


if __name__ == "__main__":
    unittest.main()
