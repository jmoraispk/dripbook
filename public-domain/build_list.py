#!/usr/bin/env python3
"""List English books Dripbook can sell as drips: public domain in the US and the EU.

Standard library only. Two steps:

  python3 public-domain/build_list.py fetch     # download Gutenberg's catalog, most downloaded first
  python3 public-domain/build_list.py build     # filter it and write public-domain/books.csv
  python3 public-domain/build_list.py build --count 200 --year 2027

`fetch` reads the Gutendex API (gutendex.com), which takes about a minute per
page of 32 books, into cache/gutendex.jsonl. It can be stopped with Ctrl-C and
resumed. `build` works offline from that file, so the rules can change without
fetching again. A book is kept only if:

  - Gutenberg marks it as not under US copyright;
  - every author and translator has a known death year, and all died at least
    71 years before the current year (EU: life + 70, from the next 1 January).
    Anonymous and "Various" books are left out, as there's no death year to check;
  - it isn't one volume of a multi-volume set;
  - it isn't another edition of a book already listed.

Translated books that name no translator are kept but flagged, because the
translation could still be under copyright. This is a first filter, not legal
clearance: before selling a book, open the Gutenberg edition and remove any
modern introduction or editor's notes.
"""
import argparse
import csv
import datetime
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache" / "gutendex.jsonl"
API = "https://gutendex.com/books/"
FIELDS = ["rank", "gutenberg_id", "title", "authors", "translators", "check_translator", "category",
          "eu_public_domain_since", "downloads_last_30_days", "gutenberg_categories", "url"]


# --- fetch ---------------------------------------------------------------

def get_json(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "dripbook-catalog/0.1"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except OSError as e:
            if attempt == tries - 1:
                raise
            print(f"  retrying after {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))


def cmd_fetch(args):
    CACHE.parent.mkdir(exist_ok=True)
    done = CACHE.read_text().splitlines() if CACHE.exists() else []
    url = json.loads(done[-1])["next"] if done else \
        API + "?" + urllib.parse.urlencode({"languages": args.language, "copyright": "false"})
    with open(CACHE, "a") as f:
        for n in range(len(done) + 1, args.pages + 1):
            if not url:
                break
            data = get_json(url)
            f.write(json.dumps({"url": url, "next": data.get("next"), "results": data["results"]}) + "\n")
            f.flush()
            print(f"  page {n}/{args.pages}", file=sys.stderr)
            url = data.get("next")
            time.sleep(1)
    print(f"Saved to {CACHE}.", file=sys.stderr)


def cached_books():
    for line in CACHE.read_text().splitlines():
        yield from json.loads(line)["results"]


# --- rules ---------------------------------------------------------------

def cutoff_year(year):
    """The latest death year that makes a work public domain in the EU in `year`."""
    return year - 71


def check(book, cutoff):
    """Return None if the book is safe to sell, or the reason it isn't."""
    if book.get("copyright") is not False:
        return "not marked public domain in the US"
    if book.get("media_type") != "Text":
        return "not a text"
    if not book.get("authors"):
        return "no named author"
    deaths = [p.get("death_year") for p in book["authors"] + book.get("translators", [])]
    if None in deaths:
        return "an author or translator has no known death year"
    if max(deaths) > cutoff:
        return "an author or translator died too recently for the EU"
    if re.search(r"\b(vol\.?|volume|part)\s+([0-9]+|[ivxlc]+)\b", book["title"], re.I):
        return "one volume of a multi-volume set"
    return None


def key(book):
    """Same work in a different Gutenberg edition gets the same key."""
    title = re.split(r"[:;\r\n]", book["title"])[0]
    title = re.sub(r"[^a-z0-9 ]", "", title.lower())
    title = re.sub(r"^(the|a|an) ", "", " ".join(title.split()))
    return (title, book["authors"][0]["name"])


# "French fiction" or a "French Literature" shelf means translated; "French Revolution" doesn't.
FOREIGN = re.compile(r"\b(french|german|russian|italian|spanish|portuguese|greek|latin|chinese|japanese|sanskrit|"
                     r"persian|arabic|norwegian|swedish|danish|dutch|polish|hindu|bengali)\s+(fiction|literature|"
                     r"poetry|drama|prose|essays|wit|letters|philosophy)\b|translations into english", re.I)


def check_translator(book):
    """True if the book looks translated but Gutenberg names no translator."""
    if book.get("translators"):
        return False
    labels = " | ".join(book.get("subjects", []) + book.get("bookshelves", []))
    ancient = any((p.get("birth_year") or 9999) < 1300 for p in book["authors"])
    return ancient or bool(FOREIGN.search(labels))


# Dripbook's own categories. Each has a pattern for Gutenberg's curated "Category: ..." shelves
# and a fallback pattern for its library subject tags. Order matters: the first match wins, so
# a novel set in history is Literature, and Meditations (Biographies + Philosophy) is Philosophy.
CATEGORIES = [
    ("Children's", r"Children & Young Adult", r"juvenile|children's stories|fairy tales|nursery rhymes"),
    ("Poetry & drama", r"Poetry|Plays/Films/Dramas", r"poetry|poems|drama|tragedies|comedies"),
    ("Literature & classics", r"Novels|Short Stories|Romance|Crime, Thrillers|Science-Fiction|Adventure|"
                              r"Historical Novels|Humour|Mythology", r"fiction|short stories|legends|folklore"),
    ("Philosophy & self-development", r"Philosophy|Religion|Psychology|How To|Parenthood|Old Age",
                                      r"philosophy|ethics|conduct of life|self-help|success|psychology|religion|"
                                      r"christian life|happiness"),
    ("Business & money", r"Economics|Business", r"economics|business|finance|money|investments|management"),
    ("Science & nature", r"Science -|Health|Nature|Engineering|Mathematics|Environmental|Nutrition|Archaeology|Drugs",
                         r"science|natural history|biology|astronomy|physics|mathematics|botany|zoology|"
                         r"medicine|health|evolution"),
    ("History & biography", r"History -|Biographies|Travel|Politics|Law", r"history|biography|autobiography|"
                                                                        r"memoirs|correspondence|travel|voyages"),
    ("Literature & classics", r"Literature|Classics|Essays|Language|Art|Music|Journalism", r"literature|essays"),
]


def category(book):
    shelves = " | ".join(s for s in book.get("bookshelves", []) if s.startswith("Category: "))
    subjects = " | ".join(book.get("subjects", []))
    for column, text in [(1, shelves), (2, subjects)]:
        for entry in CATEGORIES:
            if text and re.search(rf"\b(?:{entry[column]})", text, re.I):
                return entry[0]
    return "Other"


# --- build ---------------------------------------------------------------

def person(p):
    return f"{p['name']} ({p.get('birth_year') or '?'}-{p.get('death_year') or '?'})"


def row(rank, book):
    people = book["authors"] + book.get("translators", [])
    return {
        "rank": rank,
        "gutenberg_id": book["id"],
        "title": " ".join(book["title"].split()),
        "authors": "; ".join(person(p) for p in book["authors"]),
        "translators": "; ".join(person(p) for p in book.get("translators", [])),
        "check_translator": "yes" if check_translator(book) else "",
        "category": category(book),
        "eu_public_domain_since": max(p["death_year"] for p in people) + 71,
        "downloads_last_30_days": book.get("download_count", 0),
        "gutenberg_categories": "; ".join(s.removeprefix("Category: ") for s in book.get("bookshelves", [])
                                          if s.startswith("Category: ")),
        "url": f"https://www.gutenberg.org/ebooks/{book['id']}",
    }


def build(books, count, cutoff):
    """Keep the first `count` distinct safe books, in the order given."""
    rows, seen, skipped = [], set(), {}
    for book in books:
        reason = check(book, cutoff)
        if not reason and key(book) in seen:
            reason = "another edition of a book already listed"
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        seen.add(key(book))
        rows.append(row(len(rows) + 1, book))
        if len(rows) == count:
            break
    return rows, skipped


def cmd_build(args):
    cutoff = cutoff_year(args.year)
    rows, skipped = build(cached_books(), args.count, cutoff)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} books to {args.out} (authors and translators died in {cutoff} or earlier).")
    if len(rows) < args.count:
        print(f"  Only {len(rows)} passed: run `fetch --pages` with more pages for more.")
    for reason, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"  skipped {n}: {reason}")
    print(f"  {sum(r['check_translator'] == 'yes' for r in rows)} flagged to check the translator")
    counts = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4} {name}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download the catalog into cache/")
    f.add_argument("--pages", type=int, default=45, help="pages of 32 books (default 45, enough for 1,000)")
    f.add_argument("--language", default="en")
    b = sub.add_parser("build", help="filter the cached catalog into books.csv")
    b.add_argument("--count", type=int, default=1000)
    b.add_argument("--year", type=int, default=datetime.date.today().year,
                   help="year the books must be public domain in (default: this year)")
    b.add_argument("--out", type=Path, default=HERE / "books.csv")
    args = ap.parse_args(argv)
    {"fetch": cmd_fetch, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    main()
