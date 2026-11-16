#!/usr/bin/env python3
"""Write site/library.json: the books shown on the home page, in rows by category.

Standard library only. Reads public-domain/books.csv and, where a book has been
downloaded, counts the words in public-domain/books/{id}/pg{id}.txt so the page
can say how many days it takes. Rerun it after more books are downloaded:

  python3 scripts/build_library.py
  python3 scripts/build_library.py --per-row 24

Books flagged check_translator are left out until their translation is checked.
"""
import argparse
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOKS_CSV = ROOT / "public-domain" / "books.csv"
TEXTS = ROOT / "public-domain" / "books"
OUT = ROOT / "site" / "library.json"
TOP = 10  # the "Most read" row, shown with big rank numbers
SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor", "of", "on", "or",
         "the", "to", "with"}
# Shorter names readers know these books by.
TITLES = {3300: "The Wealth of Nations", 205: "Walden", 43: "Dr. Jekyll and Mr. Hyde",
          1080: "A Modest Proposal", 2591: "Grimms' Fairy Tales"}
ROWS = ["Literature & classics", "Philosophy & self-development", "History & biography",
        "Poetry & drama", "Children's", "Science & nature", "Business & money"]


def short_title(title):
    """'Moby Dick; Or, The Whale' -> 'Moby Dick'. Keeps a subtitle only if the title is one word."""
    title = re.sub(r"\s*:\s*\$b.*", "", title)  # library catalog leftovers
    main = re.split(r"\s*[;:]\s*|,\s+or\b|\s+—\s+", title, maxsplit=1, flags=re.I)[0].strip() or title
    words = main.split()
    if any(w[0].islower() and w.lower() not in SMALL for w in words[1:]):  # 'The divine comedy'
        words = [w if i and w.lower() in SMALL else w[0].upper() + w[1:] for i, w in enumerate(words)]
    return " ".join(words)


def author_name(authors):
    """'Austen, Jane (1775-1817); ...' -> 'Jane Austen'."""
    first = authors.split(";")[0]
    first = re.sub(r"\s*\(.*\)", "", first).strip()
    if "," in first:
        last, rest = [p.strip() for p in first.split(",", 1)]
        rest = re.sub(r",.*", "", rest)  # 'Leo, graf' -> 'Leo'
        if rest[:1].islower():  # 'Augustine, of Hippo' -> 'Augustine of Hippo'
            return f"{last} {rest}"
        return f"{rest} {last}"
    return first


def word_count(book_id):
    path = TEXTS / book_id / f"pg{book_id}.txt"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    start, end = text.find("*** START"), text.find("*** END")
    if start >= 0 and end > start:
        text = text[text.find("\n", start) + 1:end]
    return len(text.split())


def build(rows, per_row):
    usable = [r for r in rows if r["check_translator"] != "yes"]
    def book(r):
        book_id = int(r["gutenberg_id"])
        return {"id": book_id, "title": TITLES.get(book_id) or short_title(r["title"]),
                "author": author_name(r["authors"]), "category": r["category"],
                "words": word_count(r["gutenberg_id"])}

    top = usable[:TOP]
    out = {"total": len(rows), "rows": [{"name": "Most read", "top": True, "books": [book(r) for r in top]}]}
    rest = [r for r in usable if r not in top]
    for name in ROWS:
        books = [book(r) for r in rest if r["category"] == name][:per_row]
        if books:
            out["rows"].append({"name": name, "books": books})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--per-row", type=int, default=18)
    args = ap.parse_args(argv)
    with open(BOOKS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    data = build(rows, args.per_row)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    books = [b for row in data["rows"] for b in row["books"]]
    counted = sum(b["words"] is not None for b in books)
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(data['rows'])} rows, {len(books)} books, "
          f"{counted} with a word count ({len(books) - counted} not downloaded yet).")


if __name__ == "__main__":
    main()
