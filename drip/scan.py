"""Reading a scanned book from the Internet Archive: its OCR (_djvu.xml) cleaned into cut.py blocks.

The OCR knows the pages and lines, so the running heads, page numbers and Google's notices can be dropped,
words broken at line ends joined, and garbled lines and smeared pages left out. Used by the sources tool
(scripts/sources.py) and by the site, which reads a scan when a reader opens it (app.Drip.scan).
Standard library only.
"""
import datetime
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import cut

UA = "dripbook/0.1 (+https://dripbook.ai)"
IA_META = "https://archive.org/metadata/{id}"
IA_FILE = "https://archive.org/download/{id}/{name}"


def get(url, params=None, timeout=60, raw=False):
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else json.loads(data)


GOOGLE = re.compile(r"digitized by|google|this is a digital copy of a book|public domain books are our gateways|"
                    r"usage guidelines|we also ask that you", re.I)
NUMBER = re.compile(r"^(?:[ivxlcdm]+|\d{1,4})$", re.I)
END_SENTENCE = re.compile(r"[.!?:;\"”’')\]]$")


def ia_files(identifier):
    """An Archive item's title, author, year, rights and the files we read: its OCR (_djvu.xml) and PDF."""
    meta = get(IA_META.format(id=identifier))
    if not meta.get("files"):
        raise ValueError("The Archive has no files for this item.")
    files = {f["name"]: f for f in meta["files"]}
    names = set(files)
    m = meta.get("metadata", {})
    creator = m.get("creator", "")
    title = m.get("title", identifier)
    xml = next((n for n in names if n.endswith("_djvu.xml")), None)
    return {"title": title[0] if isinstance(title, list) else title,
            "author": "; ".join(creator) if isinstance(creator, list) else creator,
            "year": str(m.get("date", ""))[:4], "rights": m.get("possible-copyright-status", ""),
            "xml": xml, "xml_size": int(files[xml].get("size") or 0) if xml else 0,
            "txt": next((n for n in names if n.endswith("_djvu.txt")), None),
            "pdf": next((n for n in sorted(names) if n.endswith(".pdf") and "_text" not in n), None),
            # the item's own storage servers: archive.org/download redirects to a mirror that is sometimes down
            "hosts": [f"https://{h}{meta['dir']}/{{name}}" for h in meta.get("workable_servers", []) if meta.get("dir")]}


def download_xml(identifier, info, timeout=60):
    """The scan's OCR as bytes, from the item's own servers first."""
    if not info["xml"]:
        raise ValueError("This scan has no OCR text on the Archive.")
    err = None
    for url in info["hosts"] + [IA_FILE.format(id=identifier, name="{name}")]:
        try:
            return get(url.format(name=urllib.parse.quote(info["xml"])), raw=True, timeout=timeout)
        except OSError as e:
            err = e
    raise err


def us_public_domain(info, year_now=None):
    """The Archive says it's out of copyright, or it was published 96 or more years ago (US: 1930 and before in 2026)."""
    year_now = year_now or datetime.date.today().year
    return info.get("rights") == "NOT_IN_COPYRIGHT" or (info.get("year", "").isdigit() and int(info["year"]) <= year_now - 96)


def scan_pages(xml):
    """Each page as a list of paragraphs, each a list of lines, from the Archive's DjVu XML.
    Footnotes (paragraphs at the foot set in smaller type) come apart: [(paras, notes), ...]."""
    root = ET.fromstring(xml)
    pages = []
    for obj in root.iter("OBJECT"):
        paras = []
        for para in obj.iter("PARAGRAPH"):
            lines, heights = [], []
            for line in para.iter("LINE"):
                words = list(line.iter("WORD"))
                text = " ".join(w.text or "" for w in words).strip()
                if text:
                    lines.append(text)
                    for w in words:
                        c = [int(x) for x in (w.get("coords") or "0,0,0,0").split(",")[:4]]
                        heights.append(abs(c[1] - c[3]))
            if lines:
                heights.sort()
                paras.append((lines, heights[len(heights) // 2] if heights else 0))
        body = sorted(h for p, h in paras if len(p) > 1) or sorted(h for _, h in paras) or [0]
        usual = body[len(body) // 2]
        notes = []
        while len(paras) > 1 and usual and paras[-1][1] < 0.8 * usual:
            notes.insert(0, paras.pop())
        # Footnotes start at the first small paragraph with a mark ('1', '*', '^'); small type above it is a
        # quotation or a caption, and stays in the text.
        k = next((i for i, (n, _) in enumerate(notes) if re.match(r"^[\d*^†‡§|'\"]", n[0])), len(notes))
        paras += notes[:k]
        notes = notes[k:]
        while len(paras) > 1 and paras[-1][0][0].startswith("^"):
            notes.insert(0, paras.pop())   # '^' is the OCR's footnote mark, whatever the type size
        notes = [n for n, _ in notes]
        pages.append(([p for p, _ in paras], notes))
    return pages


def running_head(paras):
    """How many of a page's first lines are its running head ('Preface ix', 'IX Music 129', or 'IX' then
    'Music 127'), and the printed page number in it: (lines, number). (0, None) when there is none."""
    lines = [ln for p in paras[:2] for ln in p][:2]
    first = lines[0].split()
    if not first or len(first) > 9:
        return 0, None
    nums = [w.strip(".,'") for w in (first[0], first[-1]) if NUMBER.match(w.strip(".,'"))]
    rest = " ".join(w for w in first if w.strip(".,'") not in nums)
    if not nums or re.match(r"^(chapter|chap\.|book|part|section|letter|canto|appendix|lecture)\b", rest, re.I):
        return 0, None
    if len(first) == 1 and len(lines) > 1 and len(lines[1].split()) <= 5 and re.search(r"\s\d{1,4}$", lines[1]):
        return 2, lines[1].split()[-1]      # 'IX' / 'Music 127'
    if len(first) == 1 or len(paras[0]) > 1 and not (rest.isupper() or rest.istitle()):
        return 0, None
    return 1, next((n for n in nums if n.isdigit()), nums[0])


def book_words(pages):
    """How often each word appears inside a line (not first or last, where the OCR splits words) in the
    whole scan: the book's own dictionary, for telling OCR noise and split words from real ones."""
    counts = {}
    for paras, notes in pages:
        for p in paras + notes:
            for ln in p:
                for w in ln.split()[1:-1]:
                    w = re.sub(r"^\W+|\W+$", "", w).lower()
                    if w.isalpha():
                        counts[w] = counts.get(w, 0) + 1
    return counts


def glue(left, right, vocab=None):
    """Two pieces of one paragraph, across a line or page end. A word broken at the end is joined: 'pro-' +
    'fessional', and, when the OCR lost the hyphen, 'dis' + 'cussion' if the book has 'discussion' but no 'cussion'."""
    if not left:
        return right
    if left.endswith("-") and right[:1].islower() and not left.endswith("--"):
        return left[:-1] + right
    a, b = left.rsplit(" ", 1)[-1], re.sub(r"\W+$", "", right.split(" ", 1)[0])
    if vocab and a.isalpha() and b.isalpha() and b.islower() and (a + b).lower() in vocab and b not in vocab:
        return left + right
    return f"{left} {right}"


def misshapen(word):
    """'t±De', 'aBa33ii2CfiiJ&', 'rn': not a word as printed."""
    return bool(any(not ch.isalpha() and ch not in "'’-" for ch in word) or re.search(r"[a-z][A-Z]", word)
                or not re.search(r"[aeiouyAEIOUYà-ÿ]", word))


def noise(lines, vocab):
    """(words, share the book uses elsewhere, share misshapen) for some lines of OCR text."""
    words = [re.sub(r"^\W+|\W+$", "", w) for ln in lines for w in ln.split()]
    words = [w for w in words if any(ch.isalpha() for ch in w)]
    if not words:
        return 0, 1, 0
    known = sum(1 for w in words if w.isalpha() and vocab.get(w.lower(), 0) >= 2)
    return len(words), known / len(words), sum(map(misshapen, words)) / len(words)


def garbled(line, vocab):
    """OCR noise rather than words: print showing through from the other side of the page, Greek read as Latin
    letters. Fewer than a third of its words are ones the book uses elsewhere, and a third or more are misshapen.
    A quotation in Latin or German has words the book doesn't use, but well-formed ones."""
    n, known, odd = noise([line], vocab)
    return n >= 4 and known < 1 / 3 and odd >= 0.35


def smeared(paras, vocab):
    """A page scanned badly (blurred, or two pages at once): its words are mostly noise. A good page that is
    in another language, or the index, still has mostly well-formed words or the book's own."""
    n, known, odd = noise([ln for p in paras for ln in p], vocab)
    return n >= 40 and known < 0.6 and odd >= 0.3


SMALL = {"a", "an", "as", "at", "be", "by", "do", "he", "if", "in", "is", "it", "me", "my", "no", "of", "on", "or",
         "so", "to", "up", "us", "we"}
PAGE_OCR = re.compile(r"^(?=.*\d)[\dilIoOTSyz©]{1,4}$|^[iloyz]{2,3}$|^\w{1,2}$")   # page numbers misread: 'I02', 'io8', 'lyo', 'h'
HEAD_WORDS = re.compile(r"[a-z]{3,}")
CHAPTER_LABEL = re.compile(r"^(chapter|chap|book|part|section|lecture|letter|canto)\b[\s.,]*([ivxlc]+|\d+)?[\s.]*$|^[ivxlc]+\.?$", re.I)


def head_key(line):
    """A line's words, for finding running heads: 'io The Philosophy of the Beautiful CHAP.' and '134 The Philosophy
    of the Beautiful chap.' are both 'the philosophy the beautiful'. '' for lines that can't be a running head.
    A short line in capitals with no page number ('FORMAL BEAUTY.') starts with '=': the first one in the book may
    be the section's own title."""
    if len(line.split()) > 10 or CHAPTER_LABEL.match(line.strip()):
        return ""
    key = " ".join(w for w in HEAD_WORDS.findall(line.lower()) if w not in ("chap", "chapter"))
    if any(NUMBER.match(w.strip(".,'")) or PAGE_OCR.match(w.strip(".,'")) and w.strip(".,'").lower() not in SMALL
           for w in line.split()):
        return key   # a running head carries its page number, or what the OCR made of it ('io8', 'I02', 'h')
    return "=" + key if key and line.isupper() and len(line.split()) <= 6 else ""


def running_heads(pages):
    """Running heads the book repeats: the first lines that open three or more pages."""
    counts = {}
    for paras, _ in pages:
        for key in {head_key(ln) for ln in [ln for p in paras[:2] for ln in p][:2]}:
            if key:
                counts[key] = counts.get(key, 0) + 1
    return {k for k, n in counts.items() if n >= 3}


def drop_heads(paras, heads, met):
    """A page without the running heads among its first three lines, which the OCR sometimes reads after the
    first line of text. Returns (paras, the page number in the head or None). met: the '=' heads seen so far."""
    seen, number, out = 0, None, []
    for p in paras:
        keep = []
        for ln in p:
            seen += 1
            key = head_key(ln) if seen <= 3 else ""
            if key in heads and not (key[0] == "=" and key not in met):
                number = number or next((w for w in ln.split() if w.isdigit()), None)
                continue
            if key in heads:
                met.add(key)
            keep.append(ln)
        if keep:
            out.append(keep)
    return out, number


def join_lines(lines, vocab=None):
    """Lines of one paragraph as one string, joining words broken at the line end."""
    out = ""
    for ln in lines:
        out = glue(out, ln, vocab)
    out = out.replace("^", "")                            # the OCR's mark for footnote references
    pieces = out.split('"')
    if len(pieces) % 2:                                    # quotes pair up: '" heady and high-minded. "' -> '"heady…"'
        out = '"'.join(t.strip() if i % 2 else t for i, t in enumerate(pieces))
    out = re.sub(r"\s+([;:,.!?])", r"\1", out)          # 'ascendant ; but' -> 'ascendant; but'
    out = re.sub(r'([,.!?;])"(\w)', r'\1" \2', out)       # '"That may be,"said' -> '"That may be," said'
    return re.sub(r"\s+", " ", out).strip()


def titles(paras):
    """A page's paragraphs as (lines, is_title). A short block opening in capitals, like 'PREHISTORIC ORIGINS' /
    'Primitive Man' under 'CHAPTER II', is a title and a subtitle: each line its own heading."""
    for lines in paras:
        caps = next((i for i, ln in enumerate(lines) if not ln.isupper()), len(lines))
        if 0 < caps < len(lines) <= 3 and all(len(ln.split()) <= 6 and ln[:1].isupper() for ln in lines) \
                and not any(re.search(r"[,;.]$", ln) for ln in lines):
            yield lines[:caps], True
            for ln in lines[caps:]:
                yield [ln], True
        else:
            yield lines, False


AD = re.compile(r"\b\d+s\.(\s*\d+d\.)?|\b(cr\.|crown|demy|fcap\.?)\s*8vo\b|\bpost free\b|^by the same author"
                r"|(^|\.\s+)By\s+(the\s+)?[A-Z]")


def skip_back(blocks):
    """Leave out the publisher's lists of other books near the end: runs of short entries, most naming an
    author or a price ('The Fine Arts. By Professor Baldwin Brown… 3s. 6d.'), four or more to a run."""
    total, seen, cuts, run, ads, gap = sum(cut.words(t) for _, t in blocks), 0, [], None, 0, 0
    for i, (kind, text) in enumerate(blocks + [("p", "x " * 100)]):   # a long last block closes the last run
        seen += cut.words(text)
        plain = cut.plain(text)
        short = kind != "pg" and cut.words(plain) <= 70
        if seen > 0.85 * total and short and AD.search(plain):
            run = i if run is None else run
            ads, gap = ads + 1, 0
        elif run is not None and (kind == "pg" or short and gap < 3):
            gap += kind != "pg"
        else:
            if run is not None and ads >= 4:
                end = i - gap if i < len(blocks) else len(blocks)
                cuts.append((run, end))
            run, ads, gap = None, 0, 0
    for a, b in reversed(cuts):
        while a > 0 and blocks[a - 1][0] == "h":
            a -= 1   # the list's own heading ('UNIVERSITY EXTENSION MANUALS')
        blocks = blocks[:a] + blocks[b:]
    return blocks


def scan_blocks(pages):
    """cut.py blocks from a scan: running heads, page numbers and Google's pages dropped, paragraphs
    carried across page breaks, a pg block at the top of each printed page, and the index left out."""
    blocks, carry, pending, last_pg = [], None, [], 0
    vocab, heads, met = book_words(pages), running_heads(pages), set()
    for paras, notes in pages:
        flat = " ".join(" ".join(p) for p in paras)
        if not paras or GOOGLE.search(flat) and len(flat) > 400:
            continue   # a blank page, or Google's notice at the front
        if smeared(paras, vocab):
            continue
        paras = [[ln for ln in p if (not GOOGLE.search(ln) or len(ln) > 80) and not garbled(ln, vocab)] for p in paras]
        paras = [p for p in paras if p]
        notes = [n for n in ([ln for ln in n if not garbled(ln, vocab)] for n in notes) if n]
        paras, number = drop_heads(paras, heads, met)
        if not paras:
            continue
        drop, found = running_head(paras)
        number = number or found
        for _ in range(drop):
            paras[0] = paras[0][1:]
            paras = [p for p in paras if p]
        if paras and len(paras[-1]) == 1 and re.match(r"^vol\.?\s+[ivxl\d]+\b", paras[-1][0], re.I) \
                and len(paras[-1][0].split()) <= 4:
            paras = paras[:-1]   # the printer's signature: 'VOL. I. B'
        if paras and len(paras[-1]) == 1 and len(paras[-1][0].split()) <= 3:
            last = paras[-1][0].strip(".,")
            if NUMBER.match(last) or len(last) <= 3:
                number = number or (last if NUMBER.match(last) else None)
                paras = paras[:-1]   # a page number or printer's mark at the foot
        if not paras:
            continue
        if number and number.isdigit() and 0 < int(number) - last_pg <= 12:
            blocks.append(("pg", number))
            last_pg = int(number)
        elif number and number.isdigit() and not last_pg:
            last_pg = int(number)
        for lines, titled in titles(paras):
            text = join_lines(lines, vocab)
            words = len(text.split())
            if re.fullmatch(r"[IVXLC]{1,6}\.?|[A-Z]{1,2}\.?|CHA[PR]\.?,?(\s+\w{1,5}\.?)?", text):
                continue   # a stray chapter number or 'CHAP.' from a running head
            letters = sum(ch.isalpha() for ch in text)
            if letters < 0.6 * len(text.replace(" ", "")):
                continue   # OCR noise: a library stamp, a smudge, an ornament
            capitals = text.isupper() and words <= 8 and not re.match(r"^[\"'“‘]|^[A-Z]\.\s", text)   # not '"F. MAX MULLER."'
            heading = titled or len(lines) <= 2 and words <= 12 and bool(cut.HEADING.match(text) or capitals)
            if not heading and carry is not None and (not END_SENTENCE.search(carry[1]) or text[:1].islower()):
                # the same paragraph, over a page break or split line by line by the OCR
                kind, prev = carry
                joined = glue(prev, text, vocab)
                at = len(blocks) - 1 - blocks[::-1].index(carry)
                blocks[at] = carry = (kind, joined)
                continue
            blocks.extend(pending)   # the last page's footnotes, once its paragraph is finished
            pending = []
            if heading and blocks and blocks[-1][0] == "h" and re.search(r"\b(OF|AND|THE|IN|TO|FOR|ON|WITH)$", blocks[-1][1]):
                blocks[-1] = ("h", f"{blocks[-1][1]} {html.escape(text)}")   # 'A MORE COMPLETE THEORY OF' / 'BEAUTY'
            elif heading:
                blocks.append(("h", html.escape(text)))
                carry = None
            else:
                carry = ("p", html.escape(text))
                blocks.append(carry)
        pending += [("note", html.escape(join_lines(n))) for n in notes]
    blocks.extend(pending)
    blocks = skip_back(skip_front(blocks))
    # The index: the last heading named INDEX in the final tenth of the book, and all after it.
    total, seen = sum(cut.words(t) for _, t in blocks), 0
    for i, (kind, text) in enumerate(blocks):
        seen += cut.words(text)
        if kind == "h" and re.match(r"^(general\s+)?index\b", cut.plain(text), re.I) and seen > 0.9 * total:
            blocks = blocks[:i]
            break
    return blocks


ENTRY = re.compile(r"(\.\s*,?){2,}|[\s.,-](\d{1,3}|[ivxlc]{1,5})(-(\d{1,3}|[ivxlc]{1,5}))?\W*$", re.I)
FRONT = re.compile(r"^(preface|introduction|introductory|prefatory note|foreword|chapter\s+(i|1|one)\b|"
                   r"book\s+(i|1|one)\b|part\s+(i|1|one)\b)", re.I)


def skip_front(blocks):
    """Drop the title pages, series notices and contents before the book begins: everything before the first
    Preface, Introduction or Chapter I heading in the first tenth, and a Contents list right after it."""
    total, seen, start = sum(cut.words(t) for _, t in blocks), 0, 0
    for i, (kind, text) in enumerate(blocks):
        seen += cut.words(text)
        if seen > 0.1 * total:
            break
        if kind == "h" and FRONT.match(cut.plain(text)):
            start = i
            break
    blocks = blocks[start:]
    for i, (kind, text) in enumerate(blocks[:200]):
        if kind == "h" and re.match(r"^(table of )?contents\b", cut.plain(text), re.I):
            # the list runs to the next heading followed by real prose (150 words before another heading)
            for j in range(i + 1, min(len(blocks), i + 400)):
                if blocks[j][0] != "h":
                    continue
                k, after = j + 1, 0
                while k < len(blocks) and blocks[k][0] != "h" and after < 150:
                    if not ENTRY.search(cut.plain(blocks[k][1])):   # a contents line ends in its page number
                        after += cut.words(blocks[k][1])
                    k += 1
                if after >= 150:
                    while j - 1 > i and blocks[j - 1][0] == "h" and CHAPTER_LABEL.match(cut.plain(blocks[j - 1][1])):
                        j -= 1   # keep 'CHAPTER I' above 'PROLEGOMENA', but not the contents' last line
                    return blocks[:i] + blocks[j:]
            break
    return blocks


def quality(blocks):
    """(words, share of words the book uses at least twice, share misshapen) for a cleaned scan.
    Clean scans come out near (…, 0.94, 0.01); a damaged one reads below 0.85 or above 0.05."""
    lines = [cut.plain(t) for _, t in blocks]
    vocab = {}
    for ln in lines:
        for w in re.findall(r"[A-Za-z]+", ln):
            vocab[w.lower()] = vocab.get(w.lower(), 0) + 1
    return noise(lines, vocab)


def readable(blocks):
    _, known, odd = quality(blocks)
    return known >= 0.85 and odd <= 0.05


def short_title(title):
    """'The philosophy of the beautiful : being outlines… / by William Knight.' -> 'The philosophy of the beautiful'"""
    main = re.split(r"\s+/\s+", title or "")[0]
    main = re.sub(r"\s*\[[^\]]*\]", "", main)
    main = re.split(r"\s*:\s+|\s*;\s+", main)[0]
    return re.sub(r"[\s.,;:…]+$", "", main).strip() or (title or "").strip()


def short_author(author):
    """'Knight, William Angus, 1836-1916' -> 'William Angus Knight'; 'Oliphant, Mrs. (Margaret), 1828-1897' -> 'Mrs. Oliphant'"""
    a = (author or "").split(";")[0]
    a = re.sub(r",?\s*\b(\d|ca\.|b\.|d\.|active|fl\.).*$", "", a).rstrip(" .,")
    bits = re.split(r"\s*,\s*", a)
    if len(bits) == 2:
        given = re.sub(r"\s*\([^)]*\)", "", bits[1]).strip() or bits[1].strip("()")
        a = given + " " + bits[0]
    return a.strip()
