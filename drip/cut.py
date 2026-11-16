#!/usr/bin/env python3
"""Cut a Project Gutenberg plain-text book into daily parts. Standard library only.

  python3 drip/cut.py public-domain/books/1342/pg1342.txt --pace 10       # list the parts
  python3 drip/cut.py public-domain/books/1342/pg1342.txt --show 3        # print part 3 as HTML
  python3 drip/cut.py --all public-domain/books                           # check every book

clean() strips Gutenberg's header, footer and illustration tags. cut() skips short
front matter (title page, contents, preface) so part 1 opens on chapter one, then splits the
text into parts of about WORDS_PER_MINUTE x pace words, at paragraph breaks,
starting a new part at a chapter heading when the current one is long enough.
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
                   r"|(BOOK|PART|VOLUME)\s+THE\s+FIRST\b|I\.?$)", re.I)
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


def words(text):
    return len(text.split())


def paragraphs(text):
    """Paragraph blocks: (kind, text). kind is 'h' (heading), 'verse' (keep line breaks) or 'p'."""
    out = []
    for block in text.split("\n\n"):
        lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        flat = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if len(lines) <= 2 and words(flat) <= 12 and HEADING.match(flat):
            out.append(("h", flat))
        elif len(lines) > 1 and max(len(ln) for ln in lines[:-1]) < 55:
            out.append(("verse", "\n".join(ln.strip() for ln in lines)))
        else:
            out.append(("p", flat))
    return out


def split_long(block, size):
    """Split one huge paragraph at sentence ends into pieces of about size words."""
    kind, text = block
    sentences = re.split(r"(?<=[.!?;])\s+", text)
    pieces, cur = [], []
    for s in sentences:
        cur.append(s)
        if words(" ".join(cur)) >= size:
            pieces.append((kind, " ".join(cur)))
            cur = []
    if cur:
        pieces.append((kind, " ".join(cur)))
    return pieces


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
        if kind != "h" or not FIRST.match(text):
            continue
        after, j = 0, i + 1
        while j < len(blocks) and blocks[j][0] != "h" and after < 150:
            after += words(blocks[j][1])
            j += 1
        if after >= 150:
            while i > 0 and blocks[i - 1][0] == "h" and FIRST.match(blocks[i - 1][1]):
                i -= 1
            return i
    return 0


def cut(text, pace, skip_front=True):
    """Split clean text into parts, each a list of (kind, text) blocks, from chapter one on."""
    target = WORDS_PER_MINUTE * pace
    blocks = []
    found = paragraphs(text)
    for b in found[story_start(found) if skip_front else 0:]:
        blocks.extend(split_long(b, target) if words(b[1]) > 1.5 * target else [b])

    parts, cur, n = [], [], 0
    for b in blocks:
        w = words(b[1])
        new_chapter = b[0] == "h" and n >= 0.5 * target
        too_long = n + w > 1.2 * target and n >= 0.6 * target
        if cur and (new_chapter or too_long):
            parts.append(cur)
            cur, n = [], 0
        cur.append(b)
        n += w
        if n >= target and b[0] != "h":
            parts.append(cur)
            cur, n = [], 0
    if cur:
        if parts and (n < 0.3 * target or all(k == "h" for k, _ in cur)):
            parts[-1].extend(cur)
        else:
            parts.append(cur)
    return parts


def part_words(part):
    return sum(words(t) for _, t in part)


def part_heading(part):
    """The first heading in a part, for the email's subtitle; '' if there is none."""
    return next((t for k, t in part if k == "h"), "")


def inline(text):
    """Escape, then turn Gutenberg's _underscores_ into italics."""
    return re.sub(r"_([^_\n]+)_", r"<em>\1</em>", html.escape(text, quote=False))


def to_html(part, p_style="", h_style=""):
    out = []
    for kind, text in part:
        if kind == "h":
            out.append(f'<h2 style="{h_style}">{inline(text)}</h2>')
        elif kind == "verse":
            out.append(f'<p style="{p_style}">{inline(text).replace(chr(10), "<br>")}</p>')
        else:
            out.append(f'<p style="{p_style}">{inline(text)}</p>')
    return "\n".join(out)


def to_text(part):
    return "\n\n".join(t for _, t in part)


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
