#!/usr/bin/env python3
"""Find public-domain books outside Gutenberg and turn them into drips. A local prototype, not part of the site.

  python3 scripts/sources.py serve                     # search page at http://localhost:8765
  python3 scripts/sources.py search philosophy of the beautiful
  python3 scripts/sources.py preview ia:philosophybeaut00kniggoog
  python3 scripts/sources.py hathi-index               # once: HathiTrust's public-domain list (1.2 GB read, ~40 MB kept)

Two sources:
  Internet Archive  searched live (advancedsearch API). Its scans come with OCR text that knows the pages
                    and lines (_djvu.xml), so the running heads, page numbers and Google's notices can be
                    dropped and words broken at line ends joined. That text is cut with drip/cut.py like a
                    Gutenberg book.
  HathiTrust        its search and page text refuse scripts, so we search our own copy of its public list
                    (HathiFiles, rights pd = public domain everywhere, pdus = US only). To read a HathiTrust
                    book we look for the same scan on the Internet Archive, where Google's scans usually are too.

Rights: the Archive's "NOT_IN_COPYRIGHT" and HathiTrust's pd/pdus are about the US. For the EU the author must
have died 71 or more years ago; we look the death year up on Wikidata when a book is opened. Check the book
itself too: a modern introduction or translation can still be protected.

"Start drip" sends the parts to dripbook.ai/api/own, the same route as a reader's own PDF: a confirmation
email, then a part every morning. Only addresses in ALLOWED_EMAILS can sign up until launch.
Standard library only.
"""
import argparse
import csv
import datetime
import gzip
import html
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "drip"))
import cut  # noqa: E402

UA = "dripbook-sources/0.1"
IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_META = "https://archive.org/metadata/{id}"
IA_FILE = "https://archive.org/download/{id}/{name}"
HATHI_LIST = "https://www.hathitrust.org/files/hathifiles/hathi_file_list.json"
HATHI_INDEX = ROOT / "public-domain" / "cache" / "hathi_pd.tsv.gz"
HATHI_FIELDS = ["htid", "rights", "bib", "oclc", "title", "imprint", "year", "author"]
WIKIDATA = "https://www.wikidata.org/w/api.php"
SITE = "https://dripbook.ai"
PAGE = Path(__file__).with_name("sources.html")
CACHE = ROOT / "public-domain" / "cache" / "sources"
EU_CUTOFF = datetime.date.today().year - 71


def get(url, params=None, timeout=60, raw=False):
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else json.loads(data)


# --- Search ---------------------------------------------------------------------------------
def search_ia(q, rows=30):
    """Old English texts on the Internet Archive that it marks as out of copyright, or that predate 1931."""
    words = " ".join(f'"{w}"' for w in re.findall(r"[\w'-]+", q)[:8])
    query = (f"({words}) AND mediatype:texts AND (language:eng OR language:English) "
             f"AND (possible-copyright-status:NOT_IN_COPYRIGHT OR date:[1500-01-01 TO 1930-12-31])")
    data = get(IA_SEARCH, {"q": query, "fl[]": ["identifier", "title", "creator", "date", "downloads",
                                                "possible-copyright-status", "contributor"],
                           "sort[]": "downloads desc", "rows": rows, "output": "json"})
    out = []
    for d in data["response"]["docs"]:
        creator = d.get("creator", "")
        out.append({"source": "ia", "id": "ia:" + d["identifier"], "title": d.get("title", ""),
                    "author": "; ".join(creator) if isinstance(creator, list) else creator,
                    "year": str(d.get("date", ""))[:4], "downloads": d.get("downloads", 0),
                    "rights": "Not in US copyright" if d.get("possible-copyright-status") == "NOT_IN_COPYRIGHT"
                    else "Before 1931", "from": d.get("contributor", ""),
                    "url": f"https://archive.org/details/{d['identifier']}"})
    return out


def search_hathi(q, limit=30):
    """Our copy of HathiTrust's public-domain English books (see hathi_index)."""
    if not HATHI_INDEX.exists():
        return None
    words = [w.lower() for w in re.findall(r"[\w'-]+", q)]
    out = []
    with gzip.open(HATHI_INDEX, "rt", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            hay = f"{row['title']} {row['author']}".lower()
            if all(w in hay for w in words):
                out.append({"source": "hathi", "id": "ht:" + row["htid"], "title": row["title"],
                            "author": row["author"], "year": row["year"],
                            "rights": "Public domain everywhere" if row["rights"] == "pd" else "Public domain in the US",
                            "url": f"https://catalog.hathitrust.org/Record/{row['bib']}"})
                if len(out) >= limit:
                    break
    return out


def hathi_index():
    """Stream HathiTrust's newest full list and keep one row per public-domain English book."""
    files = get(HATHI_LIST)
    full = max((f for f in files if f.get("full")), key=lambda f: f["created"])
    print(f"Reading {full['filename']} ({full['size'] / 1e9:.1f} GB)…", file=sys.stderr)
    req = urllib.request.Request(full["url"], headers={"User-Agent": UA})
    seen, kept, n = set(), 0, 0
    HATHI_INDEX.parent.mkdir(parents=True, exist_ok=True)
    part = HATHI_INDEX.with_name(HATHI_INDEX.name + ".part")
    with urllib.request.urlopen(req, timeout=600) as r, gzip.open(part, "wt", encoding="utf-8") as out:
        w = csv.writer(out, delimiter="\t")
        w.writerow(HATHI_FIELDS)
        for line in io.TextIOWrapper(gzip.GzipFile(fileobj=r), encoding="utf-8", errors="replace"):
            n += 1
            c = line.rstrip("\n").split("\t")
            if len(c) < 26:
                continue
            # htid access rights bib description ... title(11) imprint(12) ... govdoc(15) date(16) ... lang(18) fmt(19) ... author(25)
            if c[2] not in ("pd", "pdus") or c[18] != "eng" or c[19] != "BK" or c[4] or c[15] == "1" or c[3] in seen:
                continue
            seen.add(c[3])
            w.writerow([c[0], c[2], c[3], c[7], c[11][:300], c[12][:120], c[16], c[25][:200]])
            kept += 1
            if n % 1_000_000 == 0:
                print(f"  {n:,} volumes read, {kept:,} books kept", file=sys.stderr)
    part.replace(HATHI_INDEX)
    print(f"Kept {kept:,} public-domain English books of {n:,} volumes -> {HATHI_INDEX}", file=sys.stderr)


def ia_twin(title, author):
    """The same book on the Internet Archive, for a HathiTrust hit: first match by title and author."""
    t = " ".join(re.findall(r"[\w'-]+", re.split(r"[/:;]", title)[0])[:8])
    last = (re.findall(r"[A-Za-z]{3,}", author) or [""])[0]
    hits = search_ia(f"{t} {last}".strip(), rows=5)
    return hits[0] if hits else None


# --- Rights ---------------------------------------------------------------------------------
def hathi_death(title, author):
    """The author's death year from HathiTrust's catalog ('Knight, William Angus, 1836-1916.'), or None."""
    last = (re.findall(r"[A-Za-z]{3,}", author.split(",")[0] if "," in author else author.split()[-1] if author else "")
            or [""])[0]
    words = re.findall(r"[A-Za-z']{3,}", re.split(r"[:;]", title)[0])[:5]
    for r in search_hathi(" ".join(words + [last]), 20) or []:
        m = re.search(r"\b(\d{3,4})\??-(\d{3,4})\b", r["author"])
        if m and last.lower() in r["author"].lower():
            return int(m.group(2))
    return None


def death_year(name, published=None):
    """Best guess from Wikidata: the death year of the first person with this name who was an adult
    and alive when the book came out (when we know the year), or None."""
    name = re.sub(r"\s*\(.*?\)|,?\s*\d{3,4}\??-.*$", "", name or "").strip()
    if "," in name:
        last, first = [p.strip() for p in name.split(",", 1)]
        name = f"{first} {last}"
    if not name:
        return None
    try:
        hits = get(WIKIDATA, {"action": "wbsearchentities", "search": name, "language": "en", "type": "item",
                              "limit": 12, "format": "json"})["search"]
        ids = [h["id"] for h in hits]
        if not ids:
            return None
        ents = get(WIKIDATA, {"action": "wbgetentities", "ids": "|".join(ids), "props": "claims", "format": "json"})
        def year_of(claims, prop):
            for c in claims.get(prop, []):
                m = re.match(r"([+-]\d+)-", c["mainsnak"].get("datavalue", {}).get("value", {}).get("time", ""))
                if m:
                    return int(m.group(1))
            return None
        for i in ids:
            claims = ents["entities"][i].get("claims", {})
            if not any(c["mainsnak"].get("datavalue", {}).get("value", {}).get("id") == "Q5"
                       for c in claims.get("P31", [])):
                continue   # not a human
            born, died = year_of(claims, "P569"), year_of(claims, "P570")
            if published and (born is None or born > published - 15 or died is not None and died < published - 1):
                continue   # a namesake: too young, or dead before the book came out
            if died is not None:
                return died
    except (OSError, ValueError, KeyError):
        return None
    return None


# --- Reading a scan -------------------------------------------------------------------------
GOOGLE = re.compile(r"digitized by|google|this is a digital copy of a book|public domain books are our gateways|"
                    r"usage guidelines|we also ask that you", re.I)
NUMBER = re.compile(r"^(?:[ivxlcdm]+|\d{1,4})$", re.I)
END_SENTENCE = re.compile(r"[.!?:;\"”’')\]]$")


def ia_files(identifier):
    meta = get(IA_META.format(id=identifier))
    if not meta.get("files"):
        raise ValueError("The Archive has no files for this item.")
    names = {f["name"] for f in meta["files"]}
    m = meta.get("metadata", {})
    creator = m.get("creator", "")
    return {"title": m.get("title", identifier), "author": "; ".join(creator) if isinstance(creator, list) else creator,
            "year": str(m.get("date", ""))[:4], "rights": m.get("possible-copyright-status", ""),
            "xml": next((n for n in names if n.endswith("_djvu.xml")), None),
            "txt": next((n for n in names if n.endswith("_djvu.txt")), None),
            "pdf": next((n for n in sorted(names) if n.endswith(".pdf") and "_text" not in n), None),
            # the item's own storage servers: archive.org/download redirects to a mirror that is sometimes down
            "hosts": [f"https://{h}{meta['dir']}/{{name}}" for h in meta.get("workable_servers", []) if meta.get("dir")]}


def fetch_ia(identifier):
    """(info, xml bytes), cached on disk."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{identifier}_djvu.xml.gz"
    meta_path = CACHE / f"{identifier}.json"
    if path.exists() and meta_path.exists():
        return json.loads(meta_path.read_text()), gzip.decompress(path.read_bytes())
    info = ia_files(identifier)
    if not info["xml"]:
        raise ValueError("This scan has no OCR text on the Archive.")
    data, err = None, None
    for url in info["hosts"] + [IA_FILE.format(id=identifier, name="{name}")]:
        try:
            data = get(url.format(name=urllib.parse.quote(info["xml"])), raw=True, timeout=60)
            break
        except OSError as e:
            err = e
    if data is None:
        raise err
    path.write_bytes(gzip.compress(data))
    meta_path.write_text(json.dumps(info))
    return info, data


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
        if notes and not re.match(r"^[\d*^†‡§|'\"]", notes[0][0][0]):
            paras += notes   # small type, but not a footnote: a quotation or a caption
            notes = []
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


def join_lines(lines):
    """Lines of one paragraph as one string, joining words broken at the line end."""
    out = ""
    for ln in lines:
        if out.endswith("-") and ln[:1].islower() and not out.endswith("--"):
            out = out[:-1] + ln
        else:
            out = f"{out} {ln}" if out else ln
    out = out.replace("^", "")                            # the OCR's mark for footnote references
    pieces = out.split('"')
    if len(pieces) % 2:                                    # quotes pair up: '" heady and high-minded. "' -> '"heady…"'
        out = '"'.join(t.strip() if i % 2 else t for i, t in enumerate(pieces))
    out = re.sub(r"\s+([;:,.!?])", r"\1", out)          # 'ascendant ; but' -> 'ascendant; but'
    out = re.sub(r'([,.!?;])"(\w)', r'\1" \2', out)       # '"That may be,"said' -> '"That may be," said'
    return re.sub(r"\s+", " ", out).strip()


def scan_blocks(pages):
    """cut.py blocks from a scan: running heads, page numbers and Google's pages dropped, paragraphs
    carried across page breaks, a pg block at the top of each printed page, and the index left out."""
    blocks, carry, pending, last_pg = [], None, [], 0
    for paras, notes in pages:
        flat = " ".join(" ".join(p) for p in paras)
        if not paras or GOOGLE.search(flat) and len(flat) > 400:
            continue   # a blank page, or Google's notice at the front
        paras = [[ln for ln in p if not GOOGLE.search(ln) or len(ln) > 80] for p in paras]
        paras = [p for p in paras if p]
        if not paras:
            continue
        drop, number = running_head(paras)
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
        for lines in paras:
            text = join_lines(lines)
            words = len(text.split())
            if re.fullmatch(r"[IVXLC]{1,6}\.?|[A-Z]\.?", text):
                continue   # a stray chapter number from a running head
            letters = sum(ch.isalpha() for ch in text)
            if letters < 0.6 * len(text.replace(" ", "")):
                continue   # OCR noise: a library stamp, a smudge, an ornament
            heading = len(lines) <= 2 and words <= 12 and bool(cut.HEADING.match(text) or text.isupper() and words <= 8)
            if not heading and carry is not None and (not END_SENTENCE.search(carry[1]) or text[:1].islower()):
                # the same paragraph, over a page break or split line by line by the OCR
                kind, prev = carry
                joined = prev[:-1] + text if prev.endswith("-") and text[:1].islower() else f"{prev} {text}"
                at = len(blocks) - 1 - blocks[::-1].index(carry)
                blocks[at] = carry = (kind, joined)
                continue
            blocks.extend(pending)   # the last page's footnotes, once its paragraph is finished
            pending = []
            if heading:
                blocks.append(("h", html.escape(text)))
                carry = None
            else:
                carry = ("p", html.escape(text))
                blocks.append(carry)
        pending += [("note", html.escape(join_lines(n))) for n in notes]
    blocks.extend(pending)
    blocks = skip_front(blocks)
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
                    while j - 1 > i and blocks[j - 1][0] == "h":   # keep 'CHAPTER I' above 'PROLEGOMENA'
                        j -= 1
                    return blocks[:i] + blocks[j:]
            break
    return blocks


def as_lessons(parts):
    """Parts in the shape the editor posts to /api/own: [[[kind, html], ...], ...]."""
    kinds = {"h": "h2", "h1": "h2", "h2": "h2", "h3": "h3", "h4": "h3"}
    return [[[kinds.get(k, k), t] for k, t in part] for part in parts]


def open_book(source_id, pace=10):
    """Everything the search page shows for one book, with its parts."""
    if source_id.startswith("ht:"):
        raise ValueError("Open the Internet Archive copy: HathiTrust doesn't let scripts read its pages.")
    identifier = source_id.removeprefix("ia:")
    info, xml = fetch_ia(identifier)
    blocks = scan_blocks(scan_pages(xml))
    parts = cut.cut_blocks(blocks, pace)
    first_author = re.split(r";", info["author"] or "")[0]
    died = None
    if first_author:
        died = hathi_death(info["title"], first_author) or \
            death_year(first_author, int(info["year"]) if info["year"].isdigit() else None)
    year = int(info["year"]) if info["year"].isdigit() else None
    rights_us = ("Not in copyright (Internet Archive)" if info["rights"] == "NOT_IN_COPYRIGHT" else
                 f"Published {year}: public domain in the US" if year and year <= datetime.date.today().year - 96 else
                 info["rights"] or "Unknown: check the publication date")
    return {"id": source_id, "title": info["title"], "author": info["author"], "year": info["year"],
            "rights_us": rights_us, "died": died,
            "eu_ok": died is not None and died <= EU_CUTOFF, "eu_cutoff": EU_CUTOFF,
            "words": sum(cut.words(t) for _, t in blocks), "pace": pace, "n_parts": len(parts),
            "pdf": info["pdf"] and IA_FILE.format(id=identifier, name=urllib.parse.quote(info["pdf"])),
            "url": f"https://archive.org/details/{identifier}",
            "parts": [{"words": cut.part_words(p), "heading": cut.plain(cut.part_heading(p) or ""),
                       "html": cut.to_html(p)} for p in parts],
            "lessons": as_lessons(parts)}


def start_drip(book, email, site=SITE):
    body = json.dumps({"email": email, "title": book["title"][:200], "lessons": book["lessons"],
                       "tz": "", "rights": True}).encode()
    req = urllib.request.Request(f"{site}/api/own", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"error": f"dripbook.ai answered {e.code}."}


# --- The local search page ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    books = {}

    def send(self, status, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        try:
            if url.path == "/":
                return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/search":
                hathi = search_hathi(q.get("q", ""))
                return self.send(200, {"ia": search_ia(q.get("q", "")), "hathi": hathi,
                                       "hathi_ready": hathi is not None})
            if url.path == "/twin":
                return self.send(200, {"twin": ia_twin(q.get("title", ""), q.get("author", ""))})
            if url.path == "/open":
                book = open_book(q["id"], int(q.get("pace", 10)))
                self.books[(book["id"], book["pace"])] = book
                return self.send(200, {k: v for k, v in book.items() if k != "lessons"})
            return self.send(404, {"error": "Not found"})
        except (OSError, ValueError, KeyError, ET.ParseError) as e:
            return self.send(502, {"error": str(e) or type(e).__name__})

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/start":
            return self.send(404, {"error": "Not found"})
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        book = self.books.get((data.get("id"), int(data.get("pace", 10))))
        if not book:
            return self.send(400, {"error": "Open the book again first."})
        status, out = start_drip(book, data.get("email", ""))
        return self.send(status, out)

    def log_message(self, *args):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    q = sub.add_parser("search")
    q.add_argument("words", nargs="+")
    p = sub.add_parser("preview")
    p.add_argument("id", help="ia:<identifier>")
    p.add_argument("--pace", type=int, default=10)
    p.add_argument("--show", type=int, default=1, help="print this part as text")
    sub.add_parser("hathi-index")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        print(f"Search page: http://localhost:{args.port}  (Ctrl-C to stop)")
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    elif args.cmd == "search":
        q = " ".join(args.words)
        for r in search_ia(q, rows=10) + (search_hathi(q, 10) or []):
            print(f"{r['id']:<45} {r['year']:<5} {r['rights']:<26} {r['title'][:60]} / {r['author'][:30]}")
    elif args.cmd == "preview":
        b = open_book(args.id, args.pace)
        print(f"{b['title']} / {b['author']} ({b['year']}): {b['words']:,} words, {b['n_parts']} parts at "
              f"{b['pace']} min. US: {b['rights_us']}. Author died: {b['died']} "
              f"({'EU ok' if b['eu_ok'] else 'check the EU'}).")
        part = cut.cut_blocks(scan_blocks(scan_pages(fetch_ia(args.id.removeprefix('ia:'))[1])), args.pace)[args.show - 1]
        print(cut.to_text(part))
    elif args.cmd == "hathi-index":
        hathi_index()


if __name__ == "__main__":
    main()
