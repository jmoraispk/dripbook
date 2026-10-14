#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic>=1.13", "pdfplumber>=0.11"]
# ///
"""Turn a non-fiction ebook PDF, or just its free sample, into a Dripbook email course.

The book is read locally into numbered blocks (headings, paragraphs, code, list
items, figures). Claude chooses where each daily lesson starts and ends, by block
number, and writes the subject line, an optional one-line lead-in and a one-line
"Tomorrow:" teaser. Lesson text is always copied from the PDF, never from Claude,
so every lesson is the author's own words. The script checks that the lessons
cover the book in order with nothing left out, except ranges marked as skipped.

  uv run converter/convert.py BOOK.pdf                          # plan with Claude, then render
  uv run converter/convert.py https://example.com/sample.pdf    # same, from a URL
  uv run converter/convert.py BOOK.pdf --pages 23-38            # use part of a PDF as the sample
  uv run converter/convert.py BOOK.pdf --plan lessons.json      # render an existing plan; no API call
  uv run converter/convert.py BOOK.pdf --blocks                 # print the numbered blocks (for writing a plan by hand)
  uv run converter/convert.py BOOK.pdf --dry-run                # print the prompts without calling the API

Output goes to converter/output/<book-slug>/: lessons.json (the full plan), an
email-ready HTML and a plain-text file for each of the first --free lessons, and
preview.html, which shows the sequence. Planning needs ANTHROPIC_API_KEY (or an
`ant auth login` profile); --plan, --blocks and --dry-run don't.
"""
import argparse
import hashlib
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "output"
USER_AGENT = "DripbookConverter/0.1 (+manual preview tool)"

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5"]
WORDS_PER_MINUTE = 220
LESSON_MINUTES = (3, 5)        # the target read time
WARN_MINUTES = (2.5, 6.5)      # outside this, the plan gets a warning
MAX_SUBJECT = 90
MAX_LINE = 220                 # lead-in, teaser and CTA note are one line each

# --------------------------------------------------------------- extraction

MONO = re.compile(r"mono|courier|consol|menlo|monaco|inconsolata|cousine|code|typewriter|fixed|"
                  r"SFTT|CMTT|LMMono|NimbusMon|Lucida ?Console|Iosevka|\bHack\b", re.I)
BOLD = re.compile(r"bold|black|heavy|semibold|demi|CMBX|-Bd\b|\.B\b", re.I)
ITALIC = re.compile(r"italic|oblique|ital|slant|CMTI|CMSL|-It\b", re.I)
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
                           "­": "", " ": " "})
BULLET = re.compile(r"^([•●▪■◦‣∙·○◆➢➤✓✔]|[-–—*](?=\s)|\d{1,2}[.)](?=\s)|[a-z][.)](?=\s)|\([a-z0-9]{1,3}\)(?=\s))\s*")
NUMBERED_HEADING = re.compile(r"^(\d+(\.\d+)*|[A-Z](\.\d+)+)\.?\s+\S")
CAPTION = re.compile(r"^(Figure|Fig\.|Table|Listing|Diagram|Exhibit|Illustration|Image)\s+[\dA-Z]+([.\-]\d+)*\s*[:.]?", re.I)
PAGE_NUMBER = re.compile(r"^\s*([0-9]{1,4}|[ivxlcdm]{1,7})\b|\b([0-9]{1,4}|[ivxlcdm]{1,7})\s*$", re.I)
TERMINAL = re.compile(r"[.!?:;)\]\"”’]$")


def font_style(fontname):
    """'code', 'b', 'i', 'bi' or '' for a PDF font name like 'ABCDEF+URWPalladioL-Bold'."""
    name = fontname.split("+")[-1]
    if MONO.search(name):
        return "code"
    return ("b" if BOLD.search(name) else "") + ("i" if ITALIC.search(name) else "")


def read_pdf(path, pages=None):
    """Characters, drawings and images of each page. The only code that needs pdfplumber.

    Each char is (text, x0, x1, top, baseline, size, fontname), in points from the top left.
    """
    import pdfplumber
    out = []
    with pdfplumber.open(path) as pdf:
        meta = pdf.metadata or {}
        total = len(pdf.pages)
        numbers = [n for n in (pages or range(1, total + 1)) if 1 <= n <= total]
        for n in numbers:
            p = pdf.pages[n - 1]
            chars = []
            for c in p.chars:
                if not c.get("upright", True):
                    continue  # rotated text: watermarks, margin notes
                m = c.get("matrix")
                base = c["bottom"] - (m[5] - c["y0"]) if m else c["bottom"]
                chars.append((c["text"], c["x0"], c["x1"], c["top"], base, c["size"], c["fontname"]))
            graphics = [(g["x0"], g["top"], g["x1"], g["bottom"], g["object_type"]) for g in p.curves + p.rects + p.lines]
            images = [(i["x0"], i["top"], i["x1"], i["bottom"]) for i in p.images]
            out.append({"number": n, "width": float(p.width), "height": float(p.height),
                        "chars": chars, "graphics": graphics, "images": images})
    return {"title": str(meta.get("Title") or "").strip(), "author": str(meta.get("Author") or "").strip(),
            "page_count": total, "pages": out}


def lines_from_chars(chars):
    """Group chars into lines, rebuilding the spaces PDFs leave out.

    Returns dicts with x0, x1, base (baseline), size, runs ([text, style] pairs,
    style being '', 'b', 'i', 'bi', 'code', 'sup' or 'sub'), text, mono (share of
    characters in a monospace font) and bold.
    """
    if not chars:
        return []
    sizes = Counter(round(c[5]) for c in chars)
    tol = 0.45 * sizes.most_common(1)[0][0]
    rows, current, anchor = [], [], None
    for c in sorted(chars, key=lambda c: (c[4], c[1])):
        if anchor is None or c[4] - anchor <= tol:
            current.append(c)
            anchor = c[4] if anchor is None else anchor
        else:
            rows.append(current)
            current, anchor = [c], c[4]
    rows.append(current)

    lines = []
    for row in rows:
        row.sort(key=lambda c: c[1])
        visible = [c for c in row if c[0].strip()]
        if not visible:
            continue
        size = Counter(round(c[5], 1) for c in visible).most_common(1)[0][0]
        base = Counter(round(c[4], 1) for c in visible if round(c[5], 1) == size).most_common(1)[0][0]
        runs, prev, pending_space = [], None, False
        counts = Counter()
        for c in row:
            text = c[0].translate(LIGATURES)
            if not text.strip():
                pending_space = prev is not None
                continue
            style = font_style(c[6])
            if c[5] < 0.85 * size and style != "code":
                if c[4] < base - 0.2 * size:
                    style = "sup"
                elif c[4] > base + 0.1 * size:
                    style = "sub"
            if prev is not None:
                gap = c[1] - prev[1][2]
                if style == "code" and prev[0] == "code" and abs(c[5] - prev[1][5]) < 0.5:
                    width = max(prev[1][2] - prev[1][1], 0.1)
                    spaces = round(gap / width) if gap > 0.4 * width else (1 if pending_space else 0)
                else:
                    spaces = 1 if (pending_space or gap > 0.15 * max(c[5], prev[1][5])) else 0
                if spaces:
                    add_text(runs, " " * spaces, style if style == prev[0] else "")
            add_text(runs, text, style)
            counts[style == "code"] += len(text)
            counts["bold"] += len(text) if "b" in style else 0
            prev, pending_space = (style, c), False
        total = counts[True] + counts[False]
        lines.append({
            "x0": round(visible[0][1], 2), "x1": round(max(c[2] for c in visible), 2), "base": base, "size": size,
            "runs": runs, "text": "".join(t for t, _ in runs),
            "mono": counts[True] / total, "bold": counts["bold"] / total,
        })
    return lines


def add_text(runs, text, style):
    if runs and runs[-1][1] == style:
        runs[-1][0] += text
    else:
        runs.append([text, style])


def figure_regions(page):
    """Boxes on the page made of drawings or images, merged when they touch."""
    w, h = page["width"], page["height"]
    items = []
    for x0, top, x1, bottom, kind in page["graphics"]:
        gw, gh = x1 - x0, bottom - top
        if (gh < 2.5 and gw > 0.25 * w) or (gw > 0.9 * w and gh > 0.9 * h) or (gw < 0.5 and gh < 0.5):
            continue  # rules under headers and above footnotes, page backgrounds, specks
        items.append([x0, top, x1, bottom, kind == "curve", False])
    for x0, top, x1, bottom in page["images"]:
        if x1 - x0 >= 16 and bottom - top >= 16:
            items.append([x0, top, x1, bottom, False, True])
    merged = True
    while merged:
        merged, out = False, []
        for it in items:
            for m in out:
                if it[0] <= m[2] + 8 and m[0] <= it[2] + 8 and it[1] <= m[3] + 8 and m[1] <= it[3] + 8:
                    m[:4] = [min(m[0], it[0]), min(m[1], it[1]), max(m[2], it[2]), max(m[3], it[3])]
                    m[4], m[5] = m[4] or it[4], m[5] or it[5]
                    merged = True
                    break
            else:
                out.append(list(it))
        items = out
    return [{"box": tuple(round(v, 1) for v in m[:4]), "curves": m[4], "image": m[5]} for m in items]


def inside(c, box, pad=2.0):
    cx, cy = (c[1] + c[2]) / 2, (c[3] + c[4]) / 2
    return box[0] - pad <= cx <= box[2] + pad and box[1] - pad <= cy <= box[3] + pad


def looks_like_text_box(lines):
    """True when the text in a drawn box is ordinary text (a code listing or a callout), not diagram labels."""
    if len(lines) < 2:
        return False
    left = min(l["x0"] for l in lines)
    return sum(abs(l["x0"] - left) < 2.5 for l in lines) >= 0.6 * len(lines)


def page_layout(page):
    """Lines of text on one page, and the figures found on it, with figure text taken out of the flow."""
    chars, figures = page["chars"], []
    for region in figure_regions(page):
        box = region["box"]
        if not region["image"] and ((box[2] - box[0]) < 40 or (box[3] - box[1]) < 25):
            continue
        within = [c for c in chars if inside(c, box)]
        if not (region["image"] or region["curves"]) or looks_like_text_box(lines_from_chars(within)):
            continue  # a box drawn around text: keep the text
        if not region["image"]:
            # labels of a drawing often sit beside or just outside its boxes: take the whole band
            box = (0, box[1] - 4, page["width"], box[3] + 4)
            beside = lines_from_chars([c for c in chars if inside(c, box, pad=0)])
            if looks_like_text_box(beside) and len(beside) > 3:
                box = region["box"]  # ordinary text runs beside it; only take what is inside
        chars = [c for c in chars if not inside(c, box, pad=0 if box[0] == 0 else 2)]
        figures.append({"top": box[1], "bottom": box[3], "kind": "image" if region["image"] else "drawing",
                        "caption": ""})
    lines = lines_from_chars(chars)
    for f in figures:  # a caption sits just below or just above its figure
        near = [l for l in lines if CAPTION.match(l["text"]) and
                (0 <= l["base"] - f["bottom"] <= 3.5 * l["size"] or 0 <= f["top"] - l["base"] <= 2.5 * l["size"])]
        if near:
            caption = [near[0]]
            for l in lines:   # a long caption wraps onto the next line or two
                if len(caption) < 3 and 0 < l["base"] - caption[-1]["base"] <= 1.5 * l["size"] \
                        and l["size"] <= caption[0]["size"] + 0.1:
                    caption.append(l)
            f["caption"] = " ".join(l["text"].strip() for l in caption)
            lines = [l for l in lines if all(l is not c for c in caption)]
    return lines, figures


def remove_furniture(pages):
    """Drop running headers, footers and page numbers. Returns the number of lines removed."""
    def band(page, line):
        return line["base"] < 0.1 * page["height"] or line["base"] > 0.92 * page["height"]

    def key(text):
        return re.sub(r"\d+", "#", text.strip().lower())

    seen = Counter()
    for page in pages:
        for k in {key(l["text"]) for l in page["lines"] if band(page, l)}:
            seen[k] += 1
    removed = 0
    for page in pages:
        keep = []
        for line in page["lines"]:
            if band(page, line) and (PAGE_NUMBER.search(line["text"]) or seen[key(line["text"])] >= 3):
                removed += 1
            else:
                keep.append(line)
        page["lines"] = keep
    return removed


# ------------------------------------------------------------------- blocks

def layout_stats(pages):
    """Body font size, line spacing and text margins, measured over the whole document."""
    prose = [l for p in pages for l in p["lines"] if l["mono"] < 0.5]
    body = Counter(round(l["size"] * 2) / 2 for l in prose for _ in range(len(l["text"]))).most_common(1)
    body = body[0][0] if body else 10.0
    deltas, code_deltas = Counter(), Counter()
    for p in pages:
        for a, b in zip(p["lines"], p["lines"][1:]):
            d = round(b["base"] - a["base"], 1)
            if 0.9 * body < d < 2.2 * body:
                if a["mono"] < 0.5 and b["mono"] < 0.5 and abs(a["size"] - body) < 0.6 and abs(b["size"] - body) < 0.6:
                    deltas[d] += 1
                elif a["mono"] >= 0.85 and b["mono"] >= 0.85:
                    code_deltas[d] += 1
    spacing = deltas.most_common(1)[0][0] if deltas else round(body * 1.2, 1)
    code_spacing = code_deltas.most_common(1)[0][0] if code_deltas else spacing
    margins = {}
    for parity in (0, 1):
        body_lines = [l for p in pages if p["number"] % 2 == parity for l in p["lines"]
                      if l["mono"] < 0.5 and abs(l["size"] - body) < 0.6]
        if body_lines:
            left = Counter(round(l["x0"]) for l in body_lines).most_common(1)[0][0]
            right = sorted(l["x1"] for l in body_lines)[int(0.9 * (len(body_lines) - 1))]
            margins[parity] = (left, right)
    fallback = next(iter(margins.values()), (72, 540))
    return {"body": body, "spacing": spacing, "code_spacing": code_spacing,
            "margins": {k: margins.get(k, fallback) for k in (0, 1)}}


def word_set(pages):
    words = Counter()
    for p in pages:
        for l in p["lines"]:
            if l["mono"] < 0.5:
                words.update(w.lower() for w in re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*", l["text"]))
    return words


def join_line(block, line, words):
    """Append a prose line to a paragraph or list item, joining words split by a hyphen at the line end."""
    runs = block["runs"]
    last_text, last_style = runs[-1]
    first_text, first_style = line["runs"][0]
    m = re.search(r"([A-Za-z]+)-$", last_text)
    n = re.match(r"([a-z]+)", first_text)
    if m and n and last_style != "code":
        joined, hyphenated = (m.group(1) + n.group(1)).lower(), f"{m.group(1)}-{n.group(1)}".lower()
        if words[hyphenated] > 0 and words[joined] == 0:
            separator = ""          # a real compound, like "floating-point"
        else:
            runs[-1][0] = last_text[:-1]
            separator = ""
    elif last_style == "code" and first_style == "code" and (last_text[-1:] in "./_-" or first_text[:1] == "/"):
        separator = ""              # a URL or path broken across lines
    else:
        separator = " "
    if separator:
        add_text(runs, separator, last_style if last_style == first_style else "")
    for text, style in line["runs"]:
        add_text(runs, text, style)


def build_blocks(pages):
    """Turn lines into blocks: heading, paragraph, code, list_item, figure and footnote.

    pages: [{"number", "width", "height", "lines", "figures"}] in reading order.
    Horizontal positions are compared relative to each page's left margin, since
    left and right pages of a book usually have different margins.
    """
    stats = layout_stats(pages)
    body, spacing, code_spacing = stats["body"], stats["spacing"], stats["code_spacing"]
    words = word_set(pages)
    heading_sizes = sorted({round(l["size"] * 2) / 2 for p in pages for l in p["lines"]
                            if is_heading_size(l, body)}, reverse=True)
    blocks, last, previous = [], None, None   # last: the block a following line may continue
    for page in pages:
        if previous is not None and page["number"] != previous + 1:
            last = None  # pages were left out in between; nothing carries over
        previous = page["number"]
        left, right = stats["margins"][page["number"] % 2]
        footnotes = footnote_lines(page, body)
        events = [(l["base"], 1, l) for l in page["lines"] if not any(l is f for f in footnotes)]
        events += [(f["top"], 0, f) for f in page["figures"]]
        prev = None
        for _, kind, item in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == 0:
                blocks.append({"type": "figure", "page": page["number"], "kind": item["kind"], "caption": item["caption"]})
                continue
            line = dict(item, rel=item["x0"] - left, long=item["x1"] >= right - 0.15 * (right - left),
                        full=item["x1"] >= right - 0.5 * body)
            gap = line["base"] - prev["base"] if prev else None
            new_page = prev is None
            prev = line
            text = line["text"].strip()
            if is_heading_size(line, body):
                level = heading_sizes.index(round(line["size"] * 2) / 2) + 1
                if (last and last["type"] == "heading" and blocks[-1] is last and not new_page
                        and (gap <= 1.5 * line["size"] or (abs(last["_size"] - line["size"]) > 0.5
                                                           and gap <= 3 * line["size"] and len(last["text"].split()) <= 3))):
                    sep = " " if gap <= 1.5 * line["size"] else "\n"
                    last["text"] += sep + text
                    last["level"] = min(last["level"], level)
                    last["_size"] = line["size"]
                    continue
                last = {"type": "heading", "page": page["number"], "level": level, "text": text, "_size": line["size"]}
                blocks.append(last)
                continue
            if (line["bold"] > 0.9 and abs(line["size"] - body) < 0.6 and NUMBERED_HEADING.match(text)
                    and len(text.split()) <= 12 and not TERMINAL.search(text) and (gap is None or gap > 1.4 * spacing)):
                last = {"type": "heading", "page": page["number"], "level": len(heading_sizes) + 1, "text": text,
                        "_size": line["size"]}
                blocks.append(last)
                continue
            prose = last if last and last["type"] in ("paragraph", "list_item") else None
            marker = list_marker(text, line, prose, gap, spacing)
            cont = (prose is not None and marker is None and not starts_definition(line)
                    and continues_prose(prose, line, gap, new_page, spacing, body))
            url_overflow = (cont and line["mono"] >= 0.85 and prose["runs"][-1][1] == "code"
                            and prose["_line"]["full"] and not prose["_line"]["text"].rstrip().endswith(":"))
            if line["mono"] >= 0.85 and not url_overflow:
                if last and last["type"] == "code" and (new_page or gap <= 2.6 * code_spacing):
                    blank = 0 if new_page else max(0, round(gap / code_spacing) - 1)
                    last["_lines"] += [None] * blank + [line]
                else:
                    last = {"type": "code", "page": page["number"], "_lines": [line]}
                    blocks.append(last)
                continue
            if cont:
                join_line(prose, line, words)
                prose["_line"] = line
                prose.setdefault("_cx", line["rel"])
                continue
            if marker:
                rest = [list(r) for r in line["runs"]]
                strip_prefix(rest, len(line["text"]) - len(line["text"].lstrip()) + BULLET.match(text).end())
                last = {"type": "list_item", "page": page["number"], "marker": marker,
                        "runs": rest, "_line": line, "_x": line["rel"]}
                blocks.append(last)
                continue
            last = {"type": "paragraph", "page": page["number"], "runs": [list(r) for r in line["runs"]],
                    "_line": line, "_x": line["rel"]}
            if line["rel"] > 1.5 * body:
                last["quote"] = True
            blocks.append(last)
        for line in footnotes:
            blocks.append({"type": "footnote", "page": page["number"], "runs": [list(r) for r in line["runs"]]})
    return finish_blocks(blocks)


def is_heading_size(line, body):
    return line["mono"] < 0.5 and line["size"] >= body * 1.12 and len(line["text"].split()) <= 20


def footnote_lines(page, body):
    """Small-print lines at the bottom of a page."""
    out = []
    for line in reversed(page["lines"]):
        if line["size"] <= 0.88 * body and line["mono"] < 0.5 and line["base"] > 0.6 * page["height"]:
            out.append(line)
        else:
            break
    return out[::-1]


SYMBOL_BULLETS = "•●▪■◦‣∙·○◆➢➤✓✔"


def next_marker(marker):
    m = re.fullmatch(r"(\(?)(\d+|[a-z])([.)])", marker)
    if not m:
        return None
    n = m.group(2)
    following = str(int(n) + 1) if n.isdigit() else chr(ord(n) + 1)
    return m.group(1) + following + m.group(3)


def list_marker(text, line, prose, gap, spacing):
    """The list marker this line starts with, or None.

    Bullets like • always start an item. A number like "3." or a dash at the start
    of a line can also be a sentence that wrapped, so it only counts when the line
    doesn't read as the next line of the paragraph above.
    """
    m = BULLET.match(text)
    if not m:
        return None
    marker = m.group(1)
    if marker in SYMBOL_BULLETS or prose is None or gap is None or gap > 1.15 * spacing:
        return marker
    prev = prose["_line"]
    if not prev["long"] or prev["text"].rstrip().endswith(":"):
        return marker
    if prose["type"] == "list_item" and next_marker(prose["marker"]) == marker:
        return marker
    return None


def starts_definition(line):
    """A line that opens with a bold term and a colon, as in a glossary or a description list."""
    runs = line["runs"]
    if not runs or "b" not in runs[0][1]:
        return False
    term = runs[0][0].strip()
    after = runs[1][0].lstrip() if len(runs) > 1 else ""
    return len(term.split()) <= 6 and (term.endswith(":") or after.startswith(":"))


def continues_prose(block, line, gap, new_page, spacing, body):
    """Does this line carry on the paragraph or list item before it?"""
    prev = block["_line"]
    tol = 0.6 * line["size"]
    if block["type"] == "list_item":
        aligned = line["rel"] > block["_x"] + 2                  # hanging indent under the marker
    elif "_cx" in block:
        aligned = abs(line["rel"] - block["_cx"]) <= tol          # same indent as earlier continuation lines
    else:
        aligned = (abs(line["rel"] - block["_x"]) <= tol          # flush paragraphs
                   or (prev["long"] and line["rel"] < block["_x"])   # first line was indented
                   or (prev["long"] and line["rel"] > block["_x"]))  # hanging indent
    if new_page:
        text = prev["text"].rstrip()
        unfinished = text.endswith("-") or not TERMINAL.search(text) or prev["full"]
        return unfinished and not text.endswith(":") and aligned
    return gap <= 1.3 * spacing * max(line["size"], prev["size"]) / body and aligned


def strip_prefix(runs, n):
    while n > 0 and runs:
        take = min(n, len(runs[0][0]))
        runs[0][0] = runs[0][0][take:]
        n -= take
        if not runs[0][0]:
            runs.pop(0)
    if runs:
        runs[0][0] = runs[0][0].lstrip()


def finish_blocks(blocks):
    """Settle code indentation, list levels, heading levels and ids; drop working fields."""
    out = []
    for b in blocks:
        if b["type"] == "code":
            base = min(l["rel"] for l in b["_lines"] if l)
            rows = []
            for l in b["_lines"]:
                if l is None:
                    rows.append("")
                    continue
                rows.append(" " * max(0, round((l["rel"] - base) / char_width(l))) + l["text"].rstrip())
            b["text"] = "\n".join(rows).strip("\n")
        if "runs" in b:
            runs = [r for r in b["runs"] if r[0]]
            if runs:
                runs[0][0] = runs[0][0].lstrip()
                runs[-1][0] = runs[-1][0].rstrip()
            b["runs"] = [r for r in runs if r[0]]
            b["text"] = "".join(t for t, _ in b["runs"])
            if all(s == "" for _, s in b["runs"]):
                del b["runs"]
        if b["type"] != "figure" and not b.get("text", "").strip():
            continue
        out.append(b)
    # list levels from indentation, within each run of list items
    run = []
    for b in out + [None]:
        if b is not None and b["type"] == "list_item":
            run.append(b)
            continue
        starts = []   # indents more than 6pt apart are different levels
        for x in sorted(i["_x"] for i in run):
            if not starts or x - starts[-1] > 6:
                starts.append(x)
        for i in run:
            i["level"] = 1 + max(k for k, x in enumerate(starts) if x <= i["_x"] + 6)
        run = []
    # heading levels as ranks of the levels actually used: 1, 2, 3...
    used = sorted({b["level"] for b in out if b["type"] == "heading"})
    counts = Counter()
    for b in out:
        if b["type"] == "heading":
            b["level"] = used.index(b["level"]) + 1
        counts[b["page"]] += 1
        b["id"] = f"p{b['page']}.{counts[b['page']]}"
        for k in [k for k in b if k.startswith("_")]:
            del b[k]
    order = ["id", "type", "page", "level", "marker", "kind", "quote", "text", "caption", "runs"]
    return [{k: b[k] for k in order if k in b} for b in out]


def char_width(line):
    """Advance width of one monospace character on this line."""
    n = len(line["text"].rstrip())
    return max((line["x1"] - line["x0"]) / n, 1.0) if n else 6.0


def extract(path, pages=None):
    """Read a PDF into blocks. Returns {"title", "author", "page_count", "pages", "blocks", "warnings"}."""
    return parse(read_pdf(path, pages))


def parse(raw):
    """Blocks from read_pdf's output. Everything from here on is plain Python, testable without a PDF."""
    laid = []
    for p in raw["pages"]:
        lines, figures = page_layout(p)
        laid.append({"number": p["number"], "width": p["width"], "height": p["height"],
                     "lines": lines, "figures": figures})
    warnings = []
    remove_furniture(laid)
    blocks = build_blocks(laid)
    unmapped = sum(b.get("text", "").count("(cid:") for b in blocks)
    if unmapped:
        warnings.append(f"{unmapped} characters couldn't be decoded from the PDF's fonts and show as (cid:N).")
    if not blocks:
        warnings.append("No text found. The PDF may be scanned images; this tool doesn't do OCR.")
    numbers = [p["number"] for p in raw["pages"]]
    return {"title": raw["title"], "author": raw["author"], "page_count": raw["page_count"],
            "pages": [min(numbers), max(numbers)] if numbers else [], "blocks": blocks, "warnings": warnings}


def block_words(block):
    if block["type"] == "figure":
        return 0
    return len(block.get("text", "").split())


# ----------------------------------------------------------------- chunking

def chunk_blocks(blocks, max_words):
    """Split the book into chapter-sized parts for planning, breaking only before headings when possible.

    Whole chapters are packed together up to max_words, so a chapter shorter than one
    lesson is planned alongside its neighbors. A chapter longer than max_words is split
    at its section headings, and failing that between blocks.
    """
    def words(seq):
        return sum(block_words(b) for b in seq)

    def split(seq, levels):
        if words(seq) <= max_words:
            return [seq]
        if not levels:
            parts, current = [], []
            for b in seq:
                if current and words(current) + block_words(b) > max_words:
                    parts.append(current)
                    current = []
                current.append(b)
            return parts + [current]
        level = levels[0]
        sections, current = [], []
        for b in seq:
            if current and b["type"] == "heading" and b["level"] <= level:
                sections.append(current)
                current = []
            current.append(b)
        sections.append(current)
        if len(sections) == 1:
            return split(seq, levels[1:])
        groups, group = [], []
        for s in sections:
            if group and words(group) + words(s) > max_words:
                groups.append(group)
                group = []
            group = group + s
        groups.append(group)
        out = []
        for g in groups:
            out.extend(split(g, levels[1:]) if words(g) > max_words else [g])
        return out

    levels = sorted({b["level"] for b in blocks if b["type"] == "heading"})
    return [c for c in split(blocks, levels) if c]


# --------------------------------------------------------------- the plan

def plan_errors(plan, blocks):
    """Structural problems that make a plan unusable. Empty means the plan is valid.

    A valid plan covers every block exactly once, in order: each block is in one
    lesson or one skipped range. Lesson text is copied from the blocks, so the only
    words a plan adds are the subject, lead-in, teaser and CTA note, each one line.
    """
    errors = []
    index = {b["id"]: i for i, b in enumerate(blocks)}
    spans = []
    lessons = plan.get("lessons") or []
    if not lessons:
        errors.append("the plan has no lessons")
    for kind, items in (("lesson", lessons), ("skipped range", plan.get("skipped") or [])):
        for n, item in enumerate(items, 1):
            label = f"{kind} {n}" + (f" ({item.get('subject')!r})" if item.get("subject") else "")
            a, b = item.get("first_block"), item.get("last_block")
            if a not in index or b not in index:
                missing = [x for x in (a, b) if x not in index]
                errors.append(f"{label}: unknown block id {', '.join(map(repr, missing))}")
                continue
            if index[a] > index[b]:
                errors.append(f"{label}: first_block {a} comes after last_block {b}")
                continue
            spans.append((index[a], index[b], kind, n, label))
    for n, lesson in enumerate(lessons, 1):
        subject = str(lesson.get("subject") or "")
        if not subject.strip():
            errors.append(f"lesson {n}: subject is empty")
        for field, limit in (("subject", MAX_SUBJECT), ("lead_in", MAX_LINE), ("teaser", MAX_LINE)):
            value = str(lesson.get(field) or "")
            if "\n" in value.strip():
                errors.append(f"lesson {n}: {field} must be one line")
            if len(value) > limit:
                errors.append(f"lesson {n}: {field} is {len(value)} characters; the limit is {limit}")
    note = str(plan.get("cta_note") or "")
    if "\n" in note.strip() or len(note) > MAX_LINE:
        errors.append(f"cta_note must be one line of at most {MAX_LINE} characters")
    lesson_starts = [s[0] for s in spans if s[2] == "lesson"]
    if lesson_starts != sorted(lesson_starts):
        errors.append("lessons are out of order: list them in the order they appear in the book")
    if errors or not blocks:
        return errors
    spans.sort()
    pos = 0
    for start, end, _, _, label in spans:
        if start > pos:
            errors.append(f"blocks {blocks[pos]['id']} to {blocks[start - 1]['id']} are in no lesson or skipped range")
        elif start < pos:
            errors.append(f"{label} overlaps the range before it (starts at {blocks[start]['id']}, "
                          f"which is already covered)")
        pos = max(pos, end + 1)
    if pos < len(blocks):
        errors.append(f"blocks {blocks[pos]['id']} to {blocks[-1]['id']} are in no lesson or skipped range")
    return errors


def lesson_warnings(lesson, blocks, index):
    """Things a reviewer should look at, without making the plan invalid."""
    out = []
    day, minutes = lesson["day"], lesson["minutes"]
    if minutes < WARN_MINUTES[0] or minutes > WARN_MINUTES[1]:
        out.append(f"Day {day} is about {minutes:g} minutes to read; the target is {LESSON_MINUTES[0]} to {LESSON_MINUTES[1]}.")
    last = blocks[index[lesson["last_block"]]]
    following = blocks[index[lesson["last_block"]] + 1] if index[lesson["last_block"]] + 1 < len(blocks) else None
    if last["type"] == "heading":
        out.append(f"Day {day} ends on a heading ({last['text']!r}).")
    if following is not None:
        if following["type"] == "code" and last.get("text", "").rstrip().endswith(":"):
            out.append(f"Day {day} ends with an introduction to code that is in the next lesson.")
        if following["type"] == "list_item" and last["type"] == "list_item":
            out.append(f"Day {day} ends in the middle of a list.")
    return out


def build_course(raw, doc, settings):
    """Attach the book's blocks to a valid plan. Returns (course, errors)."""
    blocks = doc["blocks"]
    errors = plan_errors(raw, blocks)
    if errors:
        return None, errors
    index = {b["id"]: i for i, b in enumerate(blocks)}
    lessons, warnings, missing = [], [], []
    for day, item in enumerate(sorted(raw["lessons"], key=lambda l: index[l["first_block"]]), 1):
        content = blocks[index[item["first_block"]]: index[item["last_block"]] + 1]
        words = sum(block_words(b) for b in content)
        lesson = {
            "day": day,
            "subject": " ".join(str(item["subject"]).split()),
            "lead_in": " ".join(str(item.get("lead_in") or "").split()),
            "teaser": " ".join(str(item.get("teaser") or "").split()),
            "first_block": item["first_block"], "last_block": item["last_block"],
            "pages": [content[0]["page"], content[-1]["page"]],
            "words": words, "minutes": round(words / WORDS_PER_MINUTE, 1),
            "free": day <= settings["free_lessons"],
            "blocks": content,
        }
        warnings += lesson_warnings(lesson, blocks, index)
        for b in content:
            if b["type"] == "figure":
                missing.append({"day": day, "block": b["id"], "page": b["page"], "caption": b["caption"]})
        lessons.append(lesson)
    skipped = []
    for item in sorted(raw.get("skipped") or [], key=lambda s: index[s["first_block"]]):
        content = blocks[index[item["first_block"]]: index[item["last_block"]] + 1]
        skipped.append({"first_block": item["first_block"], "last_block": item["last_block"],
                        "reason": str(item.get("reason") or ""), "pages": [content[0]["page"], content[-1]["page"]],
                        "words": sum(block_words(b) for b in content)})
    previous = {(l.get("first_block"), l.get("last_block")): l.get("blocks") for l in raw["lessons"] if l.get("blocks")}
    for lesson in lessons:
        old = previous.get((lesson["first_block"], lesson["last_block"]))
        if old is not None and [b.get("text") for b in old] != [b.get("text") for b in lesson["blocks"]]:
            warnings.append(f"Day {lesson['day']}: the text in the plan file differs from the PDF; "
                            "the emails use the PDF's text.")
    book = dict(raw.get("book") or {})
    title = settings.get("title") or book.get("title") or doc["title"] or settings["fallback_title"]
    author = settings.get("author") or book.get("author") or doc["author"]
    course = {
        "format": "dripbook-course/1",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "book": {"title": title, "author": author, "slug": slugify(title)},
        "source": settings["source"],
        "planned_by": raw.get("planned_by") or "hand",
        "settings": {k: settings[k] for k in ("free_lessons", "price", "cta_url")},
        "cta_note": " ".join(str(raw.get("cta_note") or "").split()),
        "summary": {"lessons": len(lessons), "words": sum(l["words"] for l in lessons),
                    "skipped_words": sum(s["words"] for s in skipped)},
        "figures_not_carried_over": missing,
        "skipped": skipped,
        "warnings": doc["warnings"] + warnings,
        "lessons": lessons,
    }
    return course, []


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].strip("-") or "book"


# ------------------------------------------------------------------- Claude

SYSTEM = """You split books into Dripbook email courses. A course sends one short lesson a day. Each lesson is an excerpt of the book in the author's own words: you decide where each lesson starts and ends, and you never rewrite, summarize or add to the author's text.

The book arrives as numbered blocks: headings, paragraphs, code, list items and figures. You answer with block ranges. A script copies the blocks into the emails exactly as written, so the only words you write are these, and readers see them set apart from the book's text:
- subject: the email subject line, under 60 characters. Say what the lesson teaches, in the book's own terms. No clickbait, no emoji, no "Day N".
- lead_in: optional. One sentence under 140 characters that sets up the lesson for someone opening it in their inbox. Leave it empty when the excerpt opens well on its own. It must not claim anything the excerpt doesn't say.
- teaser: one sentence under 140 characters about what the next lesson covers, written to follow "Tomorrow:". Base it only on the next lesson's blocks. Empty for the last lesson of the source text.

How to split:
- Each lesson is a 3 to 5 minute read, about 650 to 1,100 words. Each block shows its word count. Ending at a natural stopping point matters more than hitting the count exactly.
- Natural stopping points, best first: the end of a chapter or section; the end of a worked example, after the text that explains its result; the end of a subsection. Never end a lesson on a heading, between a sentence that introduces code (it often ends with a colon) and the code, between code and the sentence that explains it, or inside a list.
- Start a lesson at a heading when one is nearby.
- Keep exercises, glossaries and debugging sections. They go with their chapter's last lesson, or form a lesson of their own if long.
- A chapter or section too short to stand alone (under about 400 words) joins the lesson before or after it, whichever is closer in topic.
- Put blocks that aren't teaching material in "skipped", with a short reason: title pages, copyright, tables of contents, dedications, notes about buying or formatting the book, ads, the index. When in doubt, keep the block in a lesson.
- Every block belongs to exactly one lesson or skipped range, in order, with no gaps or overlaps.

Also fill in:
- book_title and book_author, if the blocks show them (a title page, a header); otherwise empty.
- cta_note: one sentence under 160 characters for the end of the last free lesson, saying what the rest of the book goes on to cover. Use chapter titles from a table of contents if these blocks include one. Empty if you can't tell."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "book_title": {"type": "string"},
        "book_author": {"type": "string"},
        "lessons": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "first_block": {"type": "string"}, "last_block": {"type": "string"},
                "subject": {"type": "string"}, "lead_in": {"type": "string"}, "teaser": {"type": "string"},
            },
            "required": ["first_block", "last_block", "subject", "lead_in", "teaser"],
            "additionalProperties": False,
        }},
        "skipped": {"type": "array", "items": {
            "type": "object",
            "properties": {"first_block": {"type": "string"}, "last_block": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["first_block", "last_block", "reason"],
            "additionalProperties": False,
        }},
        "cta_note": {"type": "string"},
    },
    "required": ["book_title", "book_author", "lessons", "skipped", "cta_note"],
    "additionalProperties": False,
}


class PlanError(Exception):
    pass


def block_listing(blocks):
    """The blocks as Claude sees them: id, type, word count, then the text."""
    out = []
    for b in blocks:
        kind = b["type"] + (f" level {b['level']}" if b["type"] == "heading" else "")
        if b["type"] == "figure":
            out.append(f"[{b['id']}] figure (not in the emails): {b['caption'] or 'no caption'}")
        elif b["type"] == "code":
            out.append(f"[{b['id']}] code, {len(b['text'].splitlines())} lines, {block_words(b)} words\n```\n{b['text']}\n```")
        elif b["type"] == "list_item":
            out.append(f"[{b['id']}] list item, {block_words(b)} words\n{b['marker']} {b['text']}")
        else:
            out.append(f"[{b['id']}] {kind}, {block_words(b)} words\n{b['text']}")
    return "\n\n".join(out)


def chunk_prompt(chunk, part, parts, book, done, upcoming):
    who = f"{book.get('title') or 'unknown title'}" + (f" by {book['author']}" if book.get("author") else "")
    lines = [f"Book: {who}", f"This is part {part} of {parts} of the source text, {sum(map(block_words, chunk)):,} words."]
    if done:
        lines.append(f"The course so far has {len(done)} lessons. The last one is \"{done[-1]['subject']}\".")
    if upcoming:
        preview = " ".join(b.get("text") or b.get("caption") or "" for b in upcoming)
        lines.append("After this part, the source continues with this (use it for the last lesson's teaser):\n"
                     f"<next>\n{' '.join(preview.split()[:150])} ...\n</next>")
    else:
        lines.append("This part ends the source text, so the last lesson's teaser is empty.")
    lines.append(f"<blocks>\n{block_listing(chunk)}\n</blocks>")
    lines.append(f"Plan the lessons for blocks {chunk[0]['id']} to {chunk[-1]['id']}.")
    return "\n\n".join(lines)


def call_claude(client, args, messages):
    params = dict(
        model=args.model,
        max_tokens=32000,
        system=SYSTEM,
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": args.effort, "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        # On a safety decline, rerun the request on Anthropic's recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    with client.beta.messages.stream(**params) as stream:
        return stream.get_final_message()


def plan_chunk(client, args, chunk, prompt, attempts=3):
    """Ask Claude for one part's lessons, sending back any validation errors until the plan is valid."""
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(attempts):
        response = call_claude(client, args, messages)
        if response.stop_reason == "refusal":
            raise PlanError("the model declined to plan this part")
        if response.stop_reason == "max_tokens":
            raise PlanError("the plan was cut off (max_tokens)")
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            data = json.loads(text)
            errors = plan_errors(data, chunk)
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            data, errors = None, [f"the reply wasn't the requested JSON ({e})"]
        if not errors:
            return data, response.usage
        print(f"    plan had {len(errors)} problem(s), asking again: {errors[0]}")
        messages += [{"role": "assistant", "content": response.content},
                     {"role": "user", "content": "That plan has problems:\n- " + "\n- ".join(errors)
                      + "\nReturn the whole plan for this part again with these fixed."}]
    raise PlanError(f"no valid plan after {attempts} attempts: {'; '.join(errors)}")


def plan_with_claude(doc, args, book):
    chunks = chunk_blocks(doc["blocks"], args.chunk_words)
    if args.dry_run:
        print("=== system ===\n" + SYSTEM)
        for i, chunk in enumerate(chunks, 1):
            upcoming = chunks[i][:8] if i < len(chunks) else []
            print(f"\n=== part {i} of {len(chunks)} ===\n" + chunk_prompt(chunk, i, len(chunks), book, [], upcoming))
        return None
    import anthropic
    client = anthropic.Anthropic(max_retries=4)
    plan = {"lessons": [], "skipped": [], "cta_note": "", "book": dict(book), "planned_by": args.model}
    usage = Counter()
    for i, chunk in enumerate(chunks, 1):
        print(f"  part {i} of {len(chunks)}: {chunk[0]['id']} to {chunk[-1]['id']}, "
              f"{sum(map(block_words, chunk)):,} words")
        upcoming = chunks[i][:8] if i < len(chunks) else []
        prompt = chunk_prompt(chunk, i, len(chunks), plan["book"], plan["lessons"], upcoming)
        data, u = plan_chunk(client, args, chunk, prompt)
        for field in ("input_tokens", "output_tokens"):
            usage[field] += getattr(u, field, 0) or 0
        plan["lessons"] += data["lessons"]
        plan["skipped"] += data["skipped"]
        plan["cta_note"] = plan["cta_note"] or data["cta_note"]
        plan["book"]["title"] = plan["book"].get("title") or data["book_title"]
        plan["book"]["author"] = plan["book"].get("author") or data["book_author"]
        print(f"    {len(data['lessons'])} lessons")
    print(f"  usage: {usage['input_tokens']:,} input tokens, {usage['output_tokens']:,} output tokens")
    return plan


# ---------------------------------------------------------------- rendering
# Colors and type follow site/styles.css. Emails use inline styles and tables so
# they hold up in Gmail and Outlook; the <style> block only adds dark mode and
# small-screen tweaks for the clients that read it.

C = {"paper": "#EFF2F7", "surface": "#FFFFFF", "surface2": "#F7F8FB", "ink": "#141B33", "muted": "#4A5270",
     "line": "#CDD4E2", "rule": "#141B33", "ribbon": "#F4B425", "ribbon_soft": "#FFF6DE"}
DARK = {"paper": "#0F1430", "surface": "#171E3D", "surface2": "#1C2447", "ink": "#EFF2F7", "muted": "#B4BCD2",
        "line": "#2D3760", "rule": "#3B4677", "ribbon_soft": "#2B2A2A"}
SANS = "'Schibsted Grotesk','Helvetica Neue',Helvetica,Arial,sans-serif"
SERIF = "'Newsreader',Georgia,'Times New Roman',serif"
MONO_STACK = "'SFMono-Regular',Menlo,Consolas,'Liberation Mono','Courier New',monospace"
FONTS = ("https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;"
         "1,6..72,400&family=Schibsted+Grotesk:wght@400;500;600;700;800&display=swap")


def esc(text):
    return html.escape(text, quote=True)


def runs_html(block):
    out = []
    for text, style in block.get("runs") or [[block.get("text", ""), ""]]:
        t = esc(text)
        if style == "code":
            t = (f'<code class="dm-code" style="font-family:{MONO_STACK};font-size:0.86em;background:{C["surface2"]};'
                 f'border:1px solid {C["line"]};border-radius:4px;padding:1px 4px;color:{C["ink"]}">{t}</code>')
        elif style in ("sup", "sub"):
            t = f'<{style} style="font-size:0.7em;line-height:0">{t}</{style}>'
        else:
            if "b" in style:
                t = f"<strong>{t}</strong>"
            if "i" in style:
                t = f"<em>{t}</em>"
        out.append(t)
    return "".join(out)


def block_html(block, lists):
    """One block as email HTML. lists collects consecutive list items so they render as one table."""
    t = block["type"]
    para = f"margin:0 0 18px;font-family:{SERIF};font-size:19px;line-height:1.6;color:{C['ink']}"
    if t == "heading":
        size = {1: 24, 2: 21}.get(block["level"], 18)
        text = "<br>".join(esc(x) for x in block["text"].split("\n"))
        return (f'<h{min(block["level"] + 1, 4)} class="dm-ink" style="margin:28px 0 12px;font-family:{SANS};'
                f'font-size:{size}px;line-height:1.25;font-weight:800;letter-spacing:-0.01em;color:{C["ink"]}">'
                f'{text}</h{min(block["level"] + 1, 4)}>')
    if t == "paragraph":
        if block.get("quote"):
            return (f'<p class="dm-ink dm-rule" style="{para};border-left:3px solid {C["ribbon"]};padding-left:16px">'
                    f'{runs_html(block)}</p>')
        return f'<p class="dm-ink" style="{para}">{runs_html(block)}</p>'
    if t == "footnote":
        return (f'<p class="dm-muted" style="margin:0 0 14px;font-family:{SERIF};font-size:15px;line-height:1.5;'
                f'color:{C["muted"]}">{runs_html(block)}</p>')
    if t == "code":
        return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                f'style="table-layout:fixed;width:100%;margin:0 0 20px;border-collapse:separate">'
                f'<tr><td class="dm-code" style="background:{C["surface2"]};border:1px solid {C["line"]};'
                f'border-radius:10px;padding:0">'
                f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;padding:14px 16px">'
                f'<pre class="dm-ink" style="margin:0;font-family:{MONO_STACK};font-size:14px;line-height:1.5;'
                f'color:{C["ink"]};white-space:pre;word-wrap:normal;word-break:normal;tab-size:4">'
                f'{esc(block["text"])}</pre></div></td></tr></table>')
    if t == "figure":
        caption = esc(block["caption"]) if block["caption"] else "A figure"
        return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                f'style="margin:0 0 20px"><tr><td class="dm-muted dm-line" style="border:1.5px dashed {C["line"]};'
                f'border-radius:10px;padding:14px 16px;font-family:{SANS};font-size:15px;line-height:1.5;'
                f'color:{C["muted"]}"><strong>Figure not shown.</strong> {caption} '
                f'It is on page {block["page"]} of the book.</td></tr></table>')
    if t == "list_item":
        lists.append(block)
        return ""
    return ""


def list_html(items):
    rows = []
    for b in items:
        pad = 24 * (b.get("level", 1) - 1)
        rows.append(f'<tr><td valign="top" class="dm-ink" style="padding:0 10px 10px {pad}px;width:1%;white-space:nowrap;'
                    f'font-family:{SERIF};font-size:19px;line-height:1.6;color:{C["ink"]}">{esc(b["marker"])}</td>'
                    f'<td valign="top" class="dm-ink" style="padding:0 0 10px;font-family:{SERIF};font-size:19px;'
                    f'line-height:1.6;color:{C["ink"]}">{runs_html(b)}</td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:0 0 12px">{"".join(rows)}</table>')


def body_html(blocks):
    out, lists = [], []
    for b in blocks:
        if b["type"] != "list_item" and lists:
            out.append(list_html(lists))
            lists = []
        out.append(block_html(b, lists))
    if lists:
        out.append(list_html(lists))
    return "\n".join(x for x in out if x)


def preheader(lesson):
    if lesson["lead_in"]:
        return lesson["lead_in"]
    for b in lesson["blocks"]:
        if b["type"] == "paragraph":
            return " ".join(b["text"].split()[:24])
    return lesson["subject"]


def dark_css():
    d = DARK
    rules = (f".dm-bg{{background:{d['paper']}!important}}"
             f".dm-card{{background:{d['surface']}!important;border-color:{d['rule']}!important}}"
             f".dm-ink{{color:{d['ink']}!important}} .dm-muted{{color:{d['muted']}!important}}"
             f".dm-code{{background:{d['surface2']}!important;border-color:{d['line']}!important;color:{d['ink']}!important}}"
             f".dm-line{{border-color:{d['line']}!important}} .dm-rule{{border-color:{d['rule']}!important}}"
             f".dm-soft{{background:{d['ribbon_soft']}!important}}")
    ogsc = " ".join(f"[data-ogsc] {r}" for r in re.findall(r"[^}]+}", rules))
    return f"@media (prefers-color-scheme: dark){{{rules}}}\n{ogsc}"


def email_html(lesson, course):
    book, settings = course["book"], course["settings"]
    total = len(course["lessons"])
    s = []
    lead = ""
    if lesson["lead_in"]:
        lead = (f'<p class="dm-muted" style="margin:0 0 24px;padding-bottom:20px;border-bottom:1px solid {C["line"]};'
                f'font-family:{SERIF};font-style:italic;font-size:18px;line-height:1.5;color:{C["muted"]}">'
                f'{esc(lesson["lead_in"])}</p>')
    teaser = ""
    if lesson["teaser"]:
        teaser = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:12px 0 0">'
                  f'<tr><td class="dm-rule dm-ink" style="border-top:1.5px solid {C["rule"]};padding-top:18px;'
                  f'font-family:{SANS};font-size:17px;line-height:1.5;color:{C["ink"]}">'
                  f'<strong>Tomorrow:</strong> {esc(lesson["teaser"])}</td></tr></table>')
    cta = ""
    if lesson["day"] == settings["free_lessons"]:
        note = "This was the last free lesson." + (f" {course['cta_note']}" if course["cta_note"] else "")
        price = settings["price"]
        label = f"Continue the course for ${price}" if price else "Continue the course"
        cta = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:28px 0 0">'
               f'<tr><td class="dm-soft" style="background:{C["ribbon_soft"]};border-radius:14px;padding:24px">'
               f'<p class="dm-muted" style="margin:0 0 18px;font-family:{SERIF};font-size:18px;line-height:1.55;'
               f'color:{C["muted"]}">{esc(note)}</p>'
               f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
               f'<td bgcolor="{C["ribbon"]}" style="background:{C["ribbon"]};border-radius:999px">'
               f'<a href="{esc(settings["cta_url"])}" style="display:inline-block;padding:15px 26px;font-family:{SANS};'
               f'font-size:17px;font-weight:700;line-height:1.2;color:{C["ink"]};text-decoration:none;'
               f'border-radius:999px">{esc(label)}</a></td></tr></table></td></tr></table>')
    byline = esc(book["title"]) + (f" by {esc(book['author'])}" if book["author"] else "")
    s.append(f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="format-detection" content="telephone=no, date=no, address=no, email=no, url=no">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{esc(lesson["subject"])}</title>
<!--[if mso]><noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<style>td,th,p,a,h2,h3,h4,strong,em{{font-family:Georgia,'Times New Roman',serif !important}} pre,code{{font-family:'Courier New',monospace !important}}</style><![endif]-->
<!--[if !mso]><!--><link rel="stylesheet" href="{FONTS}"><!--<![endif]-->
<style>
:root{{color-scheme:light dark;supported-color-schemes:light dark}}
body{{margin:0!important;padding:0!important;width:100%!important;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%}}
pre{{white-space:pre!important;word-wrap:normal!important}}
@media (max-width:620px){{.dm-pad{{padding:24px 18px!important}} .dm-title{{font-size:26px!important}}}}
{dark_css()}
</style>
</head>
<body class="dm-bg" style="margin:0;padding:0;background:{C["paper"]}">
<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;mso-hide:all">{esc(preheader(lesson))}{"&#8199;&#65279;&#847; " * 30}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="dm-bg" style="background:{C["paper"]}">
<tr><td align="center" style="padding:24px 10px">
<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%">
<tr><td class="dm-muted" style="padding:0 6px 12px;font-family:{SANS};font-size:14px;font-weight:600;color:{C["muted"]}">
Day {lesson["day"]} &middot; {esc(book["title"])}</td></tr>
<tr><td class="dm-card dm-pad" style="background:{C["surface"]};border:1.5px solid {C["rule"]};border-radius:20px;padding:36px 36px 32px">
<h1 class="dm-ink dm-title" style="margin:0 0 20px;font-family:{SANS};font-size:30px;line-height:1.15;font-weight:800;letter-spacing:-0.02em;color:{C["ink"]}">{esc(lesson["subject"])}</h1>
{lead}
{body_html(lesson["blocks"])}
{teaser}
{cta}
</td></tr>
<tr><td class="dm-muted" style="padding:18px 6px 0;font-family:{SANS};font-size:13px;line-height:1.6;color:{C["muted"]}">
From <em>{byline}</em>. Lesson {lesson["day"]} of {total}.<br>
<a href="{{{{UNSUBSCRIBE_URL}}}}" class="dm-muted" style="color:{C["muted"]}">Unsubscribe</a>
</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr>
</table>
</body>
</html>
""")
    return "".join(s)


def email_text(lesson, course):
    """The plain-text part of the email: the same lesson with no markup."""
    book, settings = course["book"], course["settings"]
    parts = [f"Day {lesson['day']} · {book['title']}\n{lesson['subject']}"]
    if lesson["lead_in"]:
        parts.append(lesson["lead_in"] + "\n\n---")
    items = []
    for b in lesson["blocks"]:
        if b["type"] == "list_item":
            items.append("  " * (b.get("level", 1) - 1) + f"{b['marker']} {b['text']}")
            continue
        if items:
            parts.append("\n".join(items))
            items = []
        if b["type"] == "code":
            parts.append("\n".join("    " + line if line else "" for line in b["text"].split("\n")))
        elif b["type"] == "figure":
            parts.append(f"[Figure not shown. {b['caption'] or 'A figure'} It is on page {b['page']} of the book.]")
        else:
            parts.append(b["text"])
    if items:
        parts.append("\n".join(items))
    if lesson["teaser"]:
        parts.append(f"---\n\nTomorrow: {lesson['teaser']}")
    if lesson["day"] == settings["free_lessons"]:
        note = "This was the last free lesson." + (f" {course['cta_note']}" if course["cta_note"] else "")
        label = f"Continue the course for ${settings['price']}" if settings["price"] else "Continue the course"
        parts.append(f"{note}\n{label}: {settings['cta_url']}")
    by = book["title"] + (f" by {book['author']}" if book["author"] else "")
    parts.append(f"--\nFrom {by}. Lesson {lesson['day']} of {len(course['lessons'])}.\n"
                 "Unsubscribe: {{UNSUBSCRIBE_URL}}")
    return "\n\n".join(parts) + "\n"


def lesson_snippet(lesson):
    if lesson["lead_in"]:
        return lesson["lead_in"]
    for b in lesson["blocks"]:
        if b["type"] == "paragraph":
            words = b["text"].split()
            return " ".join(words[:28]) + (" …" if len(words) > 28 else "")
    return ""


PREVIEW_CSS = """
:root{color-scheme:light;--paper:#EFF2F7;--surface:#FFFFFF;--surface-2:#F7F8FB;--ink:#141B33;--muted:#4A5270;
--line:#CDD4E2;--rule:#141B33;--ribbon:#F4B425;--ribbon-soft:#FFF6DE;--on-ribbon:#141B33;
--sans:'Schibsted Grotesk','Helvetica Neue',Helvetica,sans-serif;--serif:'Newsreader',Georgia,'Times New Roman',serif}
@media (prefers-color-scheme: dark){:root{color-scheme:dark;--paper:#0F1430;--surface:#171E3D;--surface-2:#1C2447;
--ink:#EFF2F7;--muted:#B4BCD2;--line:#2D3760;--rule:#3B4677;--ribbon-soft:#2B2A2A}}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--ink)}
.wrap{max-width:1040px;margin:0 auto;padding-inline:32px}
@media (max-width:600px){.wrap{padding-inline:16px}}
.nav{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;padding-block:24px}
.brand{display:flex;align-items:center;gap:10px;font-size:22px;font-weight:800;letter-spacing:-.02em}
.brand svg{stroke:var(--ink)}
.tag{font-size:14px;font-weight:600;color:var(--muted)}
.hero{padding-block:32px 48px;display:flex;flex-direction:column;gap:18px}
.eyebrow{font-size:15px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
h1{margin:0;font-size:clamp(38px,6vw,64px);line-height:1;font-weight:800;letter-spacing:-.035em;max-width:16ch}
h2{margin:0;font-size:clamp(28px,4vw,40px);line-height:1.08;font-weight:800;letter-spacing:-.03em}
.lede{margin:0;font-size:20px;line-height:1.5;color:var(--muted);max-width:36em}
.inbox{background:var(--surface);border:1.5px solid var(--rule);border-radius:20px;overflow:hidden}
.inbox-head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;padding:16px 24px;border-bottom:1.5px solid var(--rule);background:var(--surface-2);font-size:14px;font-weight:600}
.row{display:flex;gap:16px;padding:18px 24px;border-bottom:1px solid var(--line);align-items:flex-start;text-decoration:none;color:inherit}
.row:last-child{border-bottom:0}
a.row:hover .subject{text-decoration:underline}
.day{flex:0 0 auto;min-width:56px;text-align:center;font-size:13px;font-weight:700;background:var(--ink);color:var(--paper);border-radius:6px;padding:4px 8px}
.row-text{display:flex;flex-direction:column;gap:6px;min-width:0;flex:1}
.subject{font-size:17px;font-weight:700}
.snippet{font-family:var(--serif);font-size:17px;line-height:1.45;color:var(--muted)}
.mins{flex:0 0 auto;font-size:14px;color:var(--muted)}
.row-locked{background:var(--ribbon-soft)}
.row-locked .day{background:var(--ribbon);color:var(--on-ribbon)}
.lock{font-size:13px;font-weight:700;color:var(--muted)}
.section{padding-block:56px;display:flex;flex-direction:column;gap:28px}
.lesson{display:flex;flex-direction:column;gap:14px;padding-top:24px;border-top:1.5px solid var(--rule)}
.lesson-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px}
.lesson-head h3{margin:0;font-size:22px;font-weight:800;letter-spacing:-.01em}
.meta{font-size:15px;color:var(--muted)}
.pills{display:flex;gap:8px}
.pill{min-height:40px;padding:0 16px;border-radius:999px;border:1.5px solid var(--rule);background:var(--surface);color:var(--ink);font:inherit;font-size:15px;font-weight:600;cursor:pointer}
.pill[aria-pressed="true"]{background:var(--ink);color:var(--paper)}
.frame{display:flex;justify-content:center}
iframe{width:100%;max-width:100%;border:1.5px solid var(--line);border-radius:16px;background:#EFF2F7;transition:width .2s}
.notes{display:flex;flex-direction:column;gap:14px;max-width:44em}
.notes p,.notes li{margin:0;font-size:17px;line-height:1.6;color:var(--muted)}
.notes ul{margin:0;padding-left:22px;display:flex;flex-direction:column;gap:6px}
.notes h3{margin:12px 0 0;font-size:19px}
footer{border-top:1.5px solid var(--rule);padding-block:28px;font-size:15px;color:var(--muted)}
"""

BRAND_SVG = ('<svg width="28" height="32" viewBox="0 0 28 32" fill="none" aria-hidden="true">'
             '<path d="M3 2h15l7 7v16a2 2 0 0 1-2 2H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z" stroke-width="2.2" stroke-linejoin="round"/>'
             '<path d="M18 2v7h7" stroke-width="2.2" stroke-linejoin="round"/>'
             '<path d="M13 13v9m-3.5-3.5L13 22l3.5-3.5" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def preview_html(course, emails):
    """One page showing the whole sequence, with the free lessons rendered exactly as they will be emailed."""
    book, settings, lessons = course["book"], course["settings"], course["lessons"]
    free = settings["free_lessons"]
    pages = course["source"]["pages"]
    by = f" by {esc(book['author'])}" if book["author"] else ""
    rows = []
    for l in lessons:
        locked = l["day"] > free
        tag = "a" if not locked else "div"
        href = f' href="#day-{l["day"]}"' if not locked else ""
        extra = '<span class="lock">After purchase</span>' if locked else ""
        rows.append(f'<{tag} class="row{" row-locked" if locked else ""}"{href}><span class="day">Day {l["day"]}</span>'
                    f'<span class="row-text"><span class="subject">{esc(l["subject"])}</span>'
                    f'<span class="snippet">{esc(lesson_snippet(l))}</span>{extra}</span>'
                    f'<span class="mins">{l["minutes"]:g} min</span></{tag}>')
    frames = []
    for l in lessons[:free]:
        frames.append(f"""<article class="lesson" id="day-{l["day"]}">
<div class="lesson-head"><div><h3>Day {l["day"]}: {esc(l["subject"])}</h3>
<span class="meta">{l["words"]:,} words, about {l["minutes"]:g} minutes. From pages {l["pages"][0]}–{l["pages"][1]}.</span></div>
<div class="pills" role="group" aria-label="Width"><button type="button" class="pill" data-width="100%" aria-pressed="true">Desktop</button><button type="button" class="pill" data-width="390px" aria-pressed="false">Phone</button></div></div>
<div class="frame"><iframe title="Day {l["day"]} email" loading="lazy" srcdoc="{esc(emails[l["day"]])}"></iframe></div>
</article>""")
    notes = [f"<p>Every lesson is an excerpt of {esc(book['title'])}, copied from the PDF without edits. "
             f"The only added words are each email's subject line, the short italic intro, the "
             f"&ldquo;Tomorrow&rdquo; line and the invitation at the end of Day {free}.</p>"]
    missing = course["figures_not_carried_over"]
    if missing:
        items = "".join(f"<li>Day {f['day']}: {esc(f['caption'] or 'a figure without a caption')} (page {f['page']})</li>"
                        for f in missing)
        notes.append(f"<h3>Figures not shown yet</h3><p>These are marked in the emails with a note pointing to the "
                     f"page in the book.</p><ul>{items}</ul>")
    if course["skipped"]:
        items = "".join(f"<li>Pages {s['pages'][0]}–{s['pages'][1]}: {esc(s['reason'] or 'skipped')}</li>"
                        for s in course["skipped"])
        notes.append(f"<h3>Left out</h3><ul>{items}</ul>")
    span = f"pages {pages[0]}–{pages[1]}" if pages else "the PDF"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{esc(book["title"])}: email course preview</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<style>{PREVIEW_CSS}</style>
</head>
<body>
<header class="wrap nav"><span class="brand">{BRAND_SVG} Dripbook</span><span class="tag">Private preview</span></header>
<main>
<section class="wrap hero">
<span class="eyebrow">Email course preview</span>
<h1>{esc(book["title"])}</h1>
<p class="lede">{esc(book["title"])}{by}, as {len(lessons)} daily lessons from {span}. Readers get the first {free} free, then pay to unlock the rest.</p>
</section>
<section class="wrap">
<div class="inbox"><div class="inbox-head"><span>{len(lessons)} lessons, one a day</span><span>First {free} free{f", then ${settings['price']}" if settings["price"] else ""}</span></div>
{"".join(rows)}
</div>
</section>
<section class="wrap section">
<h2>The free lessons, as readers get them</h2>
{"".join(frames)}
</section>
<section class="wrap section notes">
<h2>How this preview was made</h2>
{"".join(notes)}
</section>
</main>
<footer><div class="wrap">Made with Dripbook. This page is private and not listed anywhere.</div></footer>
<script>
function fit(f){{try{{f.style.height=f.contentDocument.documentElement.scrollHeight+'px'}}catch(e){{}}}}
document.querySelectorAll('iframe').forEach(function(f){{f.addEventListener('load',function(){{fit(f);setTimeout(function(){{fit(f)}},600)}})}});
document.querySelectorAll('.lesson').forEach(function(a){{
  var f=a.querySelector('iframe');
  a.querySelectorAll('.pill').forEach(function(b){{b.addEventListener('click',function(){{
    a.querySelectorAll('.pill').forEach(function(x){{x.setAttribute('aria-pressed',String(x===b))}});
    f.style.width=b.dataset.width;setTimeout(function(){{fit(f)}},250);
  }})}});
}});
</script>
</body>
</html>
"""


def write_outputs(course, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in list(out_dir.glob("day-*.html")) + list(out_dir.glob("day-*.txt")):
        old.unlink()
    (out_dir / "lessons.json").write_text(json.dumps(course, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    emails = {}
    for lesson in course["lessons"][:course["settings"]["free_lessons"]]:
        stem = f"day-{lesson['day']:02d}"
        emails[lesson["day"]] = email_html(lesson, course)
        (out_dir / f"{stem}.html").write_text(emails[lesson["day"]], encoding="utf-8")
        (out_dir / f"{stem}.txt").write_text(email_text(lesson, course), encoding="utf-8")
        if len(emails[lesson["day"]].encode()) > 100_000:
            course["warnings"].append(f"Day {lesson['day']}'s HTML is over 100 KB; Gmail clips emails past 102 KB.")
    (out_dir / "preview.html").write_text(preview_html(course, emails), encoding="utf-8")


# --------------------------------------------------------------------- main

def fetch(source):
    """A local path for SOURCE, downloading it first if it's a URL. Returns (path, url or None)."""
    if not re.match(r"https?://", source, re.I):
        path = Path(source).expanduser()
        if not path.is_file():
            sys.exit(f"No such file: {source}")
        return path, None
    name = slugify(Path(urllib.parse.urlsplit(source).path).stem or "download") + ".pdf"
    dest = DEFAULT_OUT / "downloads" / name
    req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(200_000_001)
            kind = resp.headers.get("Content-Type", "unknown type")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Could not download {source}: {getattr(e, 'reason', e)}")
    if not data.startswith(b"%PDF"):
        sys.exit(f"{source} returned {kind}, not a PDF. If it's a book's sample page, open it in a browser "
                 "and copy the link to the PDF file itself.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"Downloaded {len(data) / 1e6:.1f} MB to {dest}")
    return dest, source


def parse_pages(spec):
    """'23-38' or '1-3,7' to a sorted list of page numbers (1-based, as a PDF viewer counts them)."""
    pages = set()
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not m:
            raise argparse.ArgumentTypeError(f"bad page range {part!r}; use something like 23-38 or 1-3,7")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        pages.update(range(min(a, b), max(a, b) + 1))
    return sorted(pages)


def print_blocks(blocks):
    for b in blocks:
        text = b.get("text") or b.get("caption") or ""
        first = " ".join(text.split())[:90]
        kind = b["type"] + (str(b["level"]) if b["type"] == "heading" else "")
        print(f"{b['id']:>9}  {kind:<10} {block_words(b):>4}w  {first}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="PDF file or URL")
    parser.add_argument("--pages", type=parse_pages, help="pages to use, e.g. 23-38 (default: all)")
    parser.add_argument("--plan", help="render this plan (a lessons.json) instead of asking Claude")
    parser.add_argument("--free", type=int, default=3, help="free lessons to render as emails (default: 3)")
    parser.add_argument("--out", help="output folder (default: converter/output/<book-slug>)")
    parser.add_argument("--title", help="book title, if the PDF doesn't say")
    parser.add_argument("--author", help="author name, if the PDF doesn't say")
    parser.add_argument("--price", type=int, default=49, help="course price in USD for the call to action (default: 49)")
    parser.add_argument("--cta-url", default="{{CHECKOUT_URL}}", help="link for the call to action")
    parser.add_argument("--model", default="claude-opus-5-5", choices=MODELS)
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--chunk-words", type=int, default=12000, help="words per planning request (default: 12000)")
    parser.add_argument("--blocks", action="store_true", help="print the numbered blocks and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the planning prompts and exit")
    args = parser.parse_args(argv)
    if args.free < 1:
        parser.error("--free must be at least 1")

    pdf, url = fetch(args.source)
    print(f"Reading {pdf}")
    doc = extract(pdf, args.pages)
    words = sum(map(block_words, doc["blocks"]))
    print(f"  {len(doc['blocks'])} blocks, {words:,} words, pages {doc['pages'][0]}–{doc['pages'][1]} "
          f"of {doc['page_count']}" if doc["pages"] else "  no pages")
    for w in doc["warnings"]:
        print(f"  warning: {w}")
    if args.blocks:
        print_blocks(doc["blocks"])
        return 0
    if not doc["blocks"]:
        return 1

    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    source = {"file": pdf.name, "url": url, "sha256": sha, "pages": doc["pages"], "page_count": doc["page_count"]}
    book = {"title": args.title or doc["title"], "author": args.author or doc["author"]}
    if args.plan:
        raw = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        old = (raw.get("source") or {}).get("sha256")
        if old and old != sha:
            print("  warning: the plan was made from a different PDF file; checking it against this one")
    else:
        print("Planning lessons with Claude")
        import anthropic
        try:
            raw = plan_with_claude(doc, args, book)
        except PlanError as e:
            print(f"Could not plan the course: {e}", file=sys.stderr)
            return 1
        except anthropic.APIStatusError as e:
            print(f"API error {e.status_code}: {e.message}", file=sys.stderr)
            return 1
        except anthropic.APIConnectionError as e:
            print(f"Could not reach the API: {e}", file=sys.stderr)
            return 1
        if raw is None:  # --dry-run
            return 0

    settings = {"free_lessons": min(args.free, len(raw.get("lessons") or [])) or args.free, "price": args.price,
                "cta_url": args.cta_url, "title": args.title, "author": args.author,
                "fallback_title": pdf.stem, "source": source}
    course, errors = build_course(raw, doc, settings)
    if errors:
        print("The plan doesn't fit this PDF:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        if not args.plan:
            rejected = DEFAULT_OUT / slugify(book["title"] or pdf.stem) / "rejected-plan.json"
            rejected.parent.mkdir(parents=True, exist_ok=True)
            rejected.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"Saved it to {rejected}; fix it and pass it back with --plan.", file=sys.stderr)
        return 1

    out_dir = Path(args.out) if args.out else DEFAULT_OUT / course["book"]["slug"]
    write_outputs(course, out_dir)
    s = course["summary"]
    print(f"\n{course['book']['title']}: {s['lessons']} lessons, {s['words']:,} words "
          f"({s['skipped_words']:,} skipped)")
    for l in course["lessons"]:
        mark = "free" if l["free"] else "    "
        print(f"  Day {l['day']:>2} {mark} {l['minutes']:>4.1f} min  {l['subject']}")
    for f in course["figures_not_carried_over"]:
        print(f"  figure not carried over: Day {f['day']}, page {f['page']}: {f['caption'] or '(no caption)'}")
    for w in course["warnings"]:
        print(f"  warning: {w}")
    print(f"\nWrote {out_dir}/: lessons.json, preview.html and {course['settings']['free_lessons']} emails "
          "(.html and .txt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
