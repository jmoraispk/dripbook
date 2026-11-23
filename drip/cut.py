#!/usr/bin/env python3
"""Cut a Project Gutenberg plain-text book into daily parts. Standard library only.

  python3 drip/cut.py public-domain/books/1342/pg1342.txt --pace 10       # list the parts
  python3 drip/cut.py public-domain/books/1342/pg1342.txt --show 3        # print part 3 as HTML
  python3 drip/cut.py --all public-domain/books                           # check every book

A book is a list of blocks, (kind, html): from Gutenberg's HTML edition (drip/gutenberg_html.py)
or, failing that, from the plain text (clean() strips the header, footer and illustration tags;
paragraphs() finds headings, verse and indented quotations). Kinds: h1..h4 (or h, a heading of
unknown level), p, quote, verse, note, and pg (a printed page number, never shown).

cut_blocks() skips short front matter (title page, contents, preface) so part 1 opens on chapter
one; when it can't find chapter one it still skips the title page and contents. Then it splits the book into parts of about WORDS_PER_MINUTE x pace words, at paragraph breaks,
starting a new part at a chapter heading when the current one is long enough. A part ends only where
a sentence does, so a paragraph that runs on into a quotation or a poem isn't left hanging; past
LONGEST x the target it ends anyway (verse without stops, chess notation).
Cutting is deterministic, so part n of a book at a pace is always the same text.
"""
import argparse
import html
import re
import sys
from pathlib import Path

WORDS_PER_MINUTE = 230
PACES = (5, 10, 20)

START = re.compile(r"^\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG.*$", re.M | re.I)
END = re.compile(r"^\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG.*$", re.M | re.I)
ILLUSTRATION = re.compile(r"\[(Illustration|Frontispiece)[^\]]*\]", re.I)
CREDIT = re.compile(r"^(Produced by|E-?text prepared by|Transcribed from|This e-?book was)", re.I)
FIRST = re.compile(r"^((CHAPTER|BOOK|PART|LETTER|STAVE|CANTO|VOLUME)\s+(I|1|ONE|FIRST)\b"
                   r"|(BOOK|PART|VOLUME)\s+THE\s+FIRST\b|I\.?$|I\.\s+[A-Z])", re.I)
# Front matter (title page, contents, a preface) is skipped only when chapter one starts in the
# first 5% of the book. Past that it's often the author's own prologue (The Turn of the Screw).
FRONT_MATTER_SHARE = 0.05
# Books whose front matter is the author's and worth reading: Darwin's introduction, Gulliver's letters.
KEEP_FRONT_MATTER = {"2009", "17157"}
HEADING = re.compile(
    r"^(CHAPTER|BOOK|PART|LETTER|STAVE|ACT|SCENE|CANTO|SECTION|VOLUME|EPILOGUE|PROLOGUE|PREFACE|INTRODUCTION)\b"
    r"|^[IVXLC]+\.?$", re.I)


def caption_heading(m):
    """An illustration tag goes, but a chapter heading printed inside it stays."""
    lines = [ln.strip(" []") for ln in m.group(0).splitlines()]
    heading = next((ln for ln in reversed(lines) if ln and HEADING.match(ln)), "")
    return f"\n\n{heading}\n\n" if heading else ""


def clean(raw):
    """The book's own text: no Gutenberg header, footer, credits or illustration tags."""
    text = raw.replace("\r\n", "\n").lstrip("﻿")
    start, end = START.search(text), END.search(text)
    text = text[start.end() if start else 0:end.start() if end else len(text)]
    text = ILLUSTRATION.sub(caption_heading, text)
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip() and "project gutenberg" not in p.lower()]
    while paras and CREDIT.match(paras[0].strip()):
        paras.pop(0)
    return "\n\n".join(p.rstrip() for p in paras)


def metadata(raw):
    """Title and author from Gutenberg's header, for books missing from library.json."""
    def field(name):
        m = re.search(rf"^{name}:\s*(.+)$", raw, re.M)
        return m.group(1).strip() if m else ""
    return field("Title"), field("Author")


TAG = re.compile(r"<[^>]+>")


def plain(text):
    """A block's html as plain text."""
    return html.unescape(TAG.sub("", text))


def words(text):
    return len(TAG.sub("", text).split())


def is_heading(kind):
    return kind[0] == "h"


def paragraphs(text):
    """Blocks from clean plain text. Lines indented by two or more spaces are a quotation or an
    example set off from the text, unless most of the book is indented that way."""
    raw = []
    for block in text.split("\n\n"):
        lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
        if lines:
            raw.append(lines)
    indented = [all(re.match(r"^ {2,}\S", ln) for ln in lines) for lines in raw]
    quotes_ok = sum(indented) < 0.4 * max(1, len(raw))
    out = []
    for lines, inset in zip(raw, indented):
        flat = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if len(lines) <= 2 and words(flat) <= 12 and HEADING.match(flat):
            out.append(("h", inline(flat)))
        elif len(lines) > 1 and max(len(ln.strip()) for ln in lines[:-1]) < 55:
            out.append(("verse", "\n".join(inline(ln.strip()) for ln in lines)))
        else:
            out.append(("quote" if inset and quotes_ok else "p", inline(flat)))
    return out


# A stop, then closing quotes, brackets or a footnote number: '…forth.”[351]' ends a sentence.
SENTENCE_END = re.compile(r"""[.!?…](?:[\s"'”’)\]*_\d]|\[\d{1,4}\]|\[[A-Z]\])*$""")
LONGEST = 1.5


def ends_sentence(part):
    """Whether a part's last text ends a sentence (a part of only headings or pages counts). Footnotes
    follow the paragraph that cites them, so it's that paragraph's end that counts."""
    last = next((t for k, t in reversed(part) if k not in ("pg", "img", "note") and not is_heading(k)), None)
    return last is None or bool(SENTENCE_END.search(plain(last).rstrip()))


def split_long(block, size):
    """Split one huge paragraph at sentence ends into pieces of about size words."""
    kind, text = block
    sentences = re.split(r"(?<=[.!?;])\s+", text)
    pieces, cur = [], []
    for s in sentences:
        cur.append(s)
        joined = " ".join(cur)
        # Never split inside <em>...</em>: the piece would open a tag the next one closes.
        if words(joined) >= size and all(joined.count(f"<{t}>") == joined.count(f"</{t}>") for t in ("em", "strong")):
            pieces.append((kind, joined))
            cur = []
    if cur:
        pieces.append((kind, " ".join(cur)))
    return pieces


# What Gutenberg's volunteers add to a book: their notes and credits, never the author's text.
EDITORIAL = re.compile(r"transcriber(?:['’]s|s['’]?)?\s+notes?\b|e-?text prepared by|project gutenberg|gutenberg\.org|"
                       r"pgdp\.net|distributed proofread|proofreading team", re.I)
CREDIT_LINE = re.compile(r"^\W*(produced by|e-?text prepared by|html version by|transcribed from|this e-?book was|"
                         r"digitized by)\b", re.I)
CAST = re.compile(r"\b(persons|characters|dramatis|cast)\b", re.I)


def drop_editorial(blocks):
    """The book without its transcriber's notes and e-text credits: a section headed Transcriber's
    Note(s) up to the next heading, and any short block that is a note or credit line."""
    out, skipping = [], False
    for kind, text in blocks:
        p = plain(text).strip()
        if is_heading(kind):
            skipping = bool(re.match(r"^\W*transcriber", p, re.I))
            if skipping or CREDIT_LINE.match(p):
                continue
        elif skipping or kind != "pg" and words(text) < 150 and (EDITORIAL.search(p) or CREDIT_LINE.match(p)):
            continue
        out.append((kind, text))
    return out


def title_page_end(blocks):
    """Index past the title page and contents at the start: short lines (title, 'by', publisher,
    edition) and headings with no text under them. It stops at the first paragraph of some length,
    verse or a quotation, a heading with text under it, or a play's list of characters."""
    total, seen, i = sum(words(t) for _, t in blocks), 0, 0
    while i < len(blocks):
        kind, text = blocks[i]
        if kind != "pg":
            if is_heading(kind):
                if CAST.search(plain(text)):
                    break
                after, j = 0, i + 1
                while j < len(blocks) and not is_heading(blocks[j][0]) and after < 150:
                    after += words(blocks[j][1])
                    j += 1
                if after >= 150:
                    break
            elif kind != "p" or words(text) > 12:
                break
            seen += words(text)
            if seen > FRONT_MATTER_SHARE * total:
                return 0
        i += 1
    if i >= len(blocks):
        return 0
    while i > 0 and blocks[i - 1][0] == "pg":   # keep the page number the text starts on
        i -= 1
    return i


def story_start(blocks):
    """Index of chapter one, so title pages, contents and prefaces are skipped; 0 if unsure.

    Chapter one is the first heading like 'CHAPTER I' or 'Letter 1' followed by at least
    150 words of text (entries in a table of contents are followed by the next entry).
    A 'BOOK I' heading just before it is kept.
    """
    total, seen = sum(words(t) for _, t in blocks), 0
    for i, (kind, text) in enumerate(blocks):
        if seen > FRONT_MATTER_SHARE * total:
            break
        seen += words(text)
        if not is_heading(kind) or not FIRST.match(plain(text)):
            continue
        after, j = 0, i + 1
        while j < len(blocks) and not is_heading(blocks[j][0]) and after < 150:
            after += words(blocks[j][1])
            j += 1
        if after >= 150:
            while i > 0 and (blocks[i - 1][0] == "pg" or is_heading(blocks[i - 1][0])
                             and FIRST.match(plain(blocks[i - 1][1]))):
                i -= 1
            return i
    return 0


def cut(text, pace, skip_front=True):
    """Split clean plain text into parts. See cut_blocks()."""
    return cut_blocks(paragraphs(text), pace, skip_front)


def cut_blocks(found, pace, skip_front=True):
    """Split a book's blocks into parts, each a list of (kind, html) blocks, from chapter one on."""
    target = WORDS_PER_MINUTE * pace
    blocks = []
    start = story_start(found) if skip_front else 0
    if skip_front and not start:
        start = title_page_end(found)
    for b in found[start:]:
        blocks.extend(split_long(b, target) if words(b[1]) > 1.5 * target else [b])

    parts, cur, n = [], [], 0
    for b in blocks:
        w = words(b[1])
        new_chapter = is_heading(b[0]) and n >= 0.5 * target
        too_long = n + w > 1.2 * target and n >= 0.6 * target and (ends_sentence(cur) or n + w > LONGEST * target)
        if cur and (new_chapter or too_long):
            parts.append(cur)
            cur, n = [], 0
        cur.append(b)
        n += w
        if n >= target and not is_heading(b[0]) and b[0] != "pg" and (ends_sentence(cur) or n >= LONGEST * target):
            parts.append(cur)
            cur, n = [], 0
    if cur:
        if parts and (n < 0.3 * target or all(is_heading(k) or k == "pg" for k, _ in cur)):
            parts[-1].extend(cur)
        else:
            parts.append(cur)
    return parts


def part_words(part):
    return sum(words(t) for k, t in part if k != "img")


def part_heading(part):
    """The first heading in a part, as plain text; '' if there is none."""
    return next((plain(t) for k, t in part if is_heading(k)), "")


def level(kind):
    return int(kind[1]) if len(kind) == 2 else 2


def context(parts, n, title=""):
    """Where part n (1-based) sits in the book: {'trail': [headings], 'pages': (first, last)}.

    trail is the chapter and section open where the part's reading starts, outermost first,
    leaving out the book's own title. pages are the printed pages it covers, or None when the
    edition has no page numbers. A page number comes right after the block it starts in.
    """
    def take(kind, text):
        nonlocal heads
        heads = {lv: t for lv, t in heads.items() if lv < level(kind)}
        heads[level(kind)] = plain(text)

    heads, page = {}, None
    for part in parts[:n - 1]:
        for kind, text in part:
            if kind == "pg":
                page = text
            elif is_heading(kind):
                take(kind, text)
    first, last, reading = page, page, False
    for kind, text in parts[n - 1]:
        if kind == "pg":
            if not reading:
                first = text   # the part opens on a new page
            last = text
        elif is_heading(kind):
            if not reading:
                take(kind, text)
        else:
            reading = True
    # A page number between two parts usually marks a page that began inside this part's last
    # paragraph (gutenberg_html moves numbers found mid-paragraph to after it).
    for kind, text in parts[n] if n < len(parts) else []:
        if kind != "pg":
            break
        last = text
    simple = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    trail = [t for _, t in sorted(heads.items()) if simple(t) and simple(t) != simple(title)]
    return {"trail": trail[-2:], "pages": (first, last) if first and last else None}


def inline(text):
    """Escape plain text, then turn Gutenberg's _underscores_ into italics."""
    return re.sub(r"_([^_\n]+)_", r"<em>\1</em>", html.escape(text, quote=False))


def to_html(part, styles=None):
    """A part as email html. styles maps a kind (p, h, h3, quote, verse, note) to inline CSS, and
    "img" to a function that turns a picture block into html (pictures are left out without it)."""
    st = styles or {}
    out, quote = [], []

    def end_quote():
        if quote:
            out.append(f'<blockquote style="{st.get("blockquote", "")}">' + "".join(quote) + "</blockquote>")
            quote.clear()

    for kind, text in part:
        if kind == "pg":
            continue
        if kind == "quote":
            quote.append(f'<p style="{st.get("quote", "")}">{text}</p>')
            continue
        end_quote()
        if kind == "img":
            out.append(st["img"](text) if callable(st.get("img")) else "")
        elif is_heading(kind):
            tag = "h2" if level(kind) <= 2 else "h3"
            out.append(f'<{tag} style="{st.get(tag, "")}">{text}</{tag}>')
        elif kind == "verse":
            out.append(f'<p style="{st.get("verse", "")}">{text.replace(chr(10), "<br>")}</p>')
        elif kind == "note":
            out.append(f'<p style="{st.get("note", "")}">{text}</p>')
        else:
            out.append(f'<p style="{st.get("p", "")}">{text}</p>')
    end_quote()
    return "\n".join(out)


def to_text(part):
    out = []
    for kind, text in part:
        if kind in ("pg", "img"):
            continue
        t = plain(text)
        out.append("    " + t.replace("\n", "\n    ") if kind in ("quote", "verse") else t)
    return "\n\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("path", type=Path, help="a pg{id}.txt file, or the books folder with --all")
    p.add_argument("--pace", type=int, default=10, choices=PACES, help="minutes a day")
    p.add_argument("--show", type=int, help="print this part (1-based) as HTML")
    p.add_argument("--all", action="store_true", help="summarize every books/*/pg*.txt")
    args = p.parse_args(argv)

    if args.all:
        for f in sorted(args.path.glob("*/pg*.txt")):
            parts = cut(clean(f.read_text(encoding="utf-8", errors="replace")), args.pace,
                        f.parent.name not in KEEP_FRONT_MATTER)
            sizes = [part_words(x) for x in parts]
            print(f"{f.parent.name}\t{len(parts)} parts\tmin {min(sizes)}\tmax {max(sizes)}")
        return
    parts = cut(clean(args.path.read_text(encoding="utf-8", errors="replace")), args.pace,
                args.path.parent.name not in KEEP_FRONT_MATTER)
    if args.show:
        print(to_html(parts[args.show - 1]))
        return
    for i, part in enumerate(parts, 1):
        print(f"{i:4}  {part_words(part):5} words  {part_heading(part)[:60]}")


if __name__ == "__main__":
    sys.exit(main())
