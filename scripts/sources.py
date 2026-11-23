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
import array
import bisect
import csv
import datetime
import gzip
import io
import json
import re
import sys
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "drip"))
import app  # noqa: E402
import cut  # noqa: E402
from scan import (  # noqa: E402
    download_xml, garbled, ia_files, join_lines, running_head, scan_blocks, scan_pages)

UA = "dripbook-sources/0.1"
IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_FILE = "https://archive.org/download/{id}/{name}"
HATHI_LIST = "https://www.hathitrust.org/files/hathifiles/hathi_file_list.json"
HATHI_INDEX = ROOT / "public-domain" / "cache" / "hathi_pd.tsv.gz"
HATHI_SEARCH = ROOT / "public-domain" / "cache" / "hathi-search"   # hathi_search.py build
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
    # every word in the title or the author, not anywhere in the description
    words = " AND ".join(f'(title:"{w}" OR creator:"{w}")' for w in re.findall(r"[\w'-]+", q)[:8])
    query = (f"({words}) AND mediatype:texts AND (language:eng OR language:English) "
             f"AND (possible-copyright-status:NOT_IN_COPYRIGHT OR date:[1500-01-01 TO 1930-12-31])")
    data = get(IA_SEARCH, {"q": query, "fl[]": ["identifier", "title", "creator", "date", "downloads",
                                                "possible-copyright-status", "contributor", "volume"],
                           "sort[]": "downloads desc", "rows": rows, "output": "json"})
    out = []
    for d in data["response"]["docs"]:
        creator = d.get("creator", "")
        out.append({"source": "ia", "id": "ia:" + d["identifier"], "title": d.get("title", ""),
                    "author": "; ".join(creator) if isinstance(creator, list) else creator,
                    "year": str(d.get("date", ""))[:4], "downloads": d.get("downloads", 0),
                    "rights": "Not in US copyright" if d.get("possible-copyright-status") == "NOT_IN_COPYRIGHT"
                    else "Before 1931", "from": d.get("contributor", ""), "volume": d.get("volume", ""),
                    "url": f"https://archive.org/details/{d['identifier']}"})
    return out


HATHI = {}                 # the index in memory, loaded on the first search (load_hathi)
HATHI_LOCK = threading.Lock()


def load_hathi():
    """Our HathiTrust index, held in memory: the file's lines, and a lowercase 'title author' line per
    book to search, with where each starts. About 400 MB; a search then takes well under a second."""
    with HATHI_LOCK:
        if not HATHI and HATHI_INDEX.exists():
            with gzip.open(HATHI_INDEX, "rb") as f:
                raw = f.read()
            lines = raw.split(b"\n")[1:]
            col = {name: i for i, name in enumerate(HATHI_FIELDS)}
            hays, starts, pos = [], array.array("q"), 0
            for line in lines:
                cells = line.split(b"\t")
                hay = (cells[col["title"]] + b" " + cells[col["author"]]).lower() if len(cells) == len(HATHI_FIELDS) else b""
                starts.append(pos)
                pos += len(hay) + 1
                hays.append(hay)
            HATHI.update(lines=lines, hay=b"\n".join(hays), starts=starts)
    return HATHI or None


def search_hathi(q, limit=30):
    """Our copy of HathiTrust's public-domain English books (see hathi_index): every word in the
    title or author. Looks up the longest word, then checks the rest on that book's line."""
    index = load_hathi()
    if index is None:
        return None
    words = sorted({w.lower().encode() for w in re.findall(r"[\w'-]+", q)}, key=len, reverse=True)
    if not words:
        return []
    hay, starts, out, seen, pos = index["hay"], index["starts"], [], set(), 0
    while len(out) < limit:
        pos = hay.find(words[0], pos)
        if pos < 0:
            break
        n = bisect.bisect_right(starts, pos) - 1
        pos = starts[n + 1] if n + 1 < len(starts) else len(hay)
        if n in seen:
            continue
        seen.add(n)
        end = starts[n + 1] - 1 if n + 1 < len(starts) else len(hay)
        if all(w in hay[starts[n]:end] for w in words[1:]):
            row = next(csv.reader([index["lines"][n].decode("utf-8")], delimiter="\t"))
            row = dict(zip(HATHI_FIELDS, row))
            out.append({"source": "hathi", "id": "hathi:" + row["htid"], "title": row["title"],
                        "author": row["author"], "year": row["year"],
                        "rights": "Public domain everywhere" if row["rights"] == "pd" else "Public domain in the US",
                        "url": f"https://catalog.hathitrust.org/Record/{row['bib']}"})
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


# --- Reading a scan: drip/scan.py, shared with the site's /api/scan ----------------------

def fetch_ia(identifier):
    """(info, xml bytes), cached on disk."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{identifier}_djvu.xml.gz"
    meta_path = CACHE / f"{identifier}.json"
    if path.exists() and meta_path.exists():
        return json.loads(meta_path.read_text()), gzip.decompress(path.read_bytes())
    info = ia_files(identifier)
    data = download_xml(identifier, info)
    path.write_bytes(gzip.compress(data))
    meta_path.write_text(json.dumps(info))
    return info, data



def as_lessons(parts):
    """Parts in the shape the editor posts to /api/own: [[[kind, html], ...], ...]."""
    kinds = {"h": "h2", "h1": "h2", "h2": "h2", "h3": "h3", "h4": "h3"}
    return [[[kinds.get(k, k), t] for k, t in part] for part in parts]


def open_book(source_id, pace=10):
    """Everything the search page shows for one book, with its parts."""
    if source_id.startswith("hathi:"):
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


# --- The site's /api/scan, on this machine -----------------------------------------------------
class LocalArchive:
    """The Archive for app.Drip, through the disk cache (fetch_ia)."""
    def ia_files(self, identifier):
        info, data = fetch_ia(identifier)
        return {**info, "xml_size": info.get("xml_size") or len(data)}

    def download_xml(self, identifier, info, timeout=60):
        return fetch_ia(identifier)[1]


class MemoryRedis:
    """Enough of Redis for reading scans: GET and SET, kept while the tool runs."""
    def __init__(self):
        self.data = {}

    def __call__(self, cmd, *args):
        if cmd == "GET":
            return self.data.get(args[0])
        if cmd == "SET":
            self.data[args[0]] = args[1]
            return "OK"
        raise app.Problem(503, f"The local tool can't {cmd}: sign-up works on the live site only.")


LOCAL_DRIP = app.Drip(MemoryRedis(), None, env={}, ia=LocalArchive())


# --- The local search page ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    books = {}

    def send(self, status, body, kind="application/json", anyone=False):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        if anyone:   # read-only files, for the website's search box (site/home.js)
            self.send_header("Access-Control-Allow-Origin", "*")
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
            if url.path.startswith("/hathi-search/"):
                return self.send_slice(url.path.removeprefix("/hathi-search/"))
            if url.path == "/api/scan":   # for the site on localhost (site/home.js SCAN_API)
                try:
                    out = (LOCAL_DRIP.scan_preview if q.get("step") == "read" else LOCAL_DRIP.scan_find)(q.get("id", ""))
                except app.Problem as e:
                    return self.send(e.status, {"error": e.message}, anyone=True)
                return self.send(200, out, anyone=True)
            if url.path == "/twin":
                return self.send(200, {"twin": ia_twin(q.get("title", ""), q.get("author", ""))})
            if url.path == "/open":
                book = open_book(q["id"], int(q.get("pace", 10)))
                self.books[(book["id"], book["pace"])] = book
                return self.send(200, {k: v for k, v in book.items() if k != "lessons"})
            return self.send(404, {"error": "Not found"})
        except (OSError, ValueError, KeyError, ET.ParseError) as e:
            return self.send(502, {"error": str(e) or type(e).__name__})

    def send_slice(self, name):
        """The HathiTrust search files (hathi_search.py build), by byte range like R2, for the site's search box."""
        path = (HATHI_SEARCH / name).resolve()
        if not path.is_relative_to(HATHI_SEARCH.resolve()) or not path.is_file():
            return self.send(404, {"error": "Not found"}, anyone=True)
        size = path.stat().st_size
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if not m:
            return self.send(200, path.read_bytes(), "application/json" if name.endswith(".json") else "text/plain", anyone=True)
        a, b = int(m[1]), min(int(m[2] or size - 1), size - 1)
        with open(path, "rb") as f:
            f.seek(a)
            data = f.read(max(0, b - a + 1))
        self.send_response(206)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.end_headers()

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
    s.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from another machine")
    q = sub.add_parser("search")
    q.add_argument("words", nargs="+")
    p = sub.add_parser("preview")
    p.add_argument("id", help="ia:<identifier>")
    p.add_argument("--pace", type=int, default=10)
    p.add_argument("--show", type=int, default=1, help="print this part as text")
    sub.add_parser("hathi-index")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        print(f"Search page: http://{args.host}:{args.port}  (Ctrl-C to stop)")
        threading.Thread(target=load_hathi, daemon=True).start()
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
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
