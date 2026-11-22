#!/usr/bin/env python3
"""Write site/catalog.json: every other English Gutenberg book we could drip, for search.

The home page lists the 1,000 most read (site/library.json). Search also looks through
this file, and a book found here gets a "Request book" button instead of a sign-up.

  python3 scripts/build_catalog.py            # uses public-domain/cache/pg_catalog.csv.gz
  python3 scripts/build_catalog.py --fetch    # downloads a fresh copy first (5 MB)

The catalog is Gutenberg's own feed, read from its mirror. A book is kept with the same
rules as public-domain/build_list.py: every author, translator, editor and other writer has
a known death year in the EU cutoff or earlier, it isn't one volume of a set, and it isn't
another edition of a book already kept. Books that look translated but name no translator
are left out, since nobody checks them by hand here. Illustrators and other picture makers
don't count: drips carry only the text. The feed has no US copyright flag; the few
Gutenberg books still under US copyright are by authors who died too recently anyway.
"""
import argparse
import csv
import datetime
import gzip
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "public-domain"))
import build_list  # noqa: E402
from build_library import author_name, short_title  # noqa: E402

FEED = "https://gutenberg.pglaf.org/cache/generated/feeds/pg_catalog.csv.gz"
CACHE = ROOT / "public-domain" / "cache" / "pg_catalog.csv.gz"
LIBRARY = ROOT / "site" / "library.json"
OUT = ROOT / "site" / "catalog.json"
# Roles whose work isn't in a text-only drip.
PICTURES = {"Illustrator", "Engraver", "Photographer", "Artist", "Composer", "Arranger", "Publisher", "Printer",
            "Transcriber", "Researcher"}
YEARS = re.compile(r",\s*(?:(\d+)\??\s*(BCE)?)?\s*-\s*(\d+)\??\s*(BCE)?$")
BORN = re.compile(r",\s*(\d+)\??\s*(BCE)?\s*-")


def year(digits, bce):
    return -int(digits) if bce else int(digits)


def person(entry):
    """'Knight, William Angus, 1836-1916 [Editor]' -> ('Editor', {'name', 'birth_year', 'death_year'})."""
    m = re.search(r"\s*\[([^\]]+)\]\s*$", entry)
    role = m.group(1) if m else "Author"
    entry = entry[:m.start()] if m else entry
    died = YEARS.search(entry)
    born = BORN.search(entry)
    name = entry[:died.start()] if died else re.sub(r",\s*(active\s+)?[\d?]+.*$", "", entry)
    return role, {"name": name.strip(),
                  "birth_year": year(born.group(1), born.group(2)) if born else None,
                  "death_year": year(died.group(3), died.group(4)) if died else None}


def as_book(row):
    """A catalog row in the shape build_list.py's rules expect (Gutendex's)."""
    authors, translators, others = [], [], []
    for entry in filter(None, (e.strip() for e in row["Authors"].split(";"))):
        role, p = person(entry)
        if role in PICTURES:
            continue
        {"Author": authors, "Translator": translators}.get(role, others).append(p)
    title = "\n".join(" ".join(line.split()) for line in row["Title"].splitlines() if line.strip())
    return {"id": int(row["Text#"]), "title": title, "authors": authors,
            "translators": translators, "others": others, "copyright": False, "media_type": row["Type"],
            "subjects": [s.strip() for s in row["Subjects"].split(";") if s.strip()],
            "bookshelves": [s.strip() for s in row["Bookshelves"].split(";") if s.strip()]}


def eligible(book, cutoff):
    """None if we could drip it, or the reason not."""
    if any(p["death_year"] is None or p["death_year"] > cutoff for p in book["others"]):
        return "an editor or other writer died too recently, or has no known death year"
    reason = build_list.check(book, cutoff)
    if reason:
        return reason
    if build_list.check_translator(book):
        return "looks translated, but names no translator"
    return None


def read_feed(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r["Language"] == "en"]


def build(rows, library_ids, cutoff):
    """[[id, title, author, subtitle], ...] for books not in the library, oldest Gutenberg number first."""
    books = sorted((as_book(r) for r in rows), key=lambda b: b["id"])
    by_id = {b["id"]: b for b in books}
    seen = {build_list.key(by_id[i]) for i in library_ids if i in by_id and by_id[i]["authors"]}
    out, skipped = [], {}
    for b in books:
        if b["id"] in library_ids:
            continue
        reason = eligible(b, cutoff)
        if not reason and build_list.key(b) in seen:
            reason = "another edition of a book already listed"
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        seen.add(build_list.key(b))
        first, _, more = b["title"].partition("\n")
        title = short_title(first)
        rest = first[len(title):] if first.lower().startswith(title.lower()) else ""
        rest = " ".join(f"{rest} {more}".split()).lstrip(" :;,.—")
        author = "; ".join(f"{p['name']} ({p['birth_year']}-{p['death_year']})" for p in b["authors"])
        out.append([b["id"], title, author_name(author), rest[:90]])
    return out, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fetch", action="store_true", help="download a fresh catalog from Gutenberg's mirror")
    ap.add_argument("--year", type=int, default=datetime.date.today().year)
    args = ap.parse_args(argv)
    if args.fetch or not CACHE.exists():
        CACHE.parent.mkdir(exist_ok=True)
        req = urllib.request.Request(FEED, headers={"User-Agent": "dripbook-catalog/0.1"})
        with urllib.request.urlopen(req, timeout=300) as r:
            CACHE.write_bytes(r.read())
    library_ids = {int(i) for i in json.loads(LIBRARY.read_text(encoding="utf-8"))["books"]}
    cutoff = build_list.cutoff_year(args.year)
    books, skipped = build(read_feed(CACHE), library_ids, cutoff)
    OUT.write_text(json.dumps({"total": len(books), "books": books}, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"Wrote {len(books):,} books to {OUT} ({OUT.stat().st_size / 1e6:.1f} MB), "
          f"besides the {len(library_ids)} in the library.")
    for reason, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"  skipped {n:,}: {reason}")


if __name__ == "__main__":
    main()
