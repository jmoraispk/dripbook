#!/usr/bin/env python3
"""Write site/library.json and site/covers/: the books and rows on the home page.

Reads public-domain/books.csv, the Gutenberg catalog cached by build_list.py
(for shelves, subjects and summaries) and the downloaded books in
public-domain/books/ (for word counts and cover images). Rerun it after the
list or the downloads change:

  python3 scripts/build_library.py
  python3 scripts/build_library.py --force-covers   # redo every cover image

Needs Pillow for the covers (`uv run --with pillow scripts/build_library.py`);
without it the page falls back to printed cloth covers. Books flagged
check_translator are left out until their translation is checked.
"""
import argparse
import csv
import io
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PD = ROOT / "public-domain"
BOOKS_CSV = PD / "books.csv"
CATALOG = PD / "cache" / "gutendex.jsonl"
TEXTS = PD / "books"
OUT = ROOT / "site" / "library.json"
COVERS = ROOT / "site" / "covers"
COVER_WIDTH = 300
WORDS_PER_MORNING = 2300  # ten minutes at 230 words a minute
PER_ROW = 18
SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor", "of", "on", "or",
         "the", "to", "with"}
# Shorter names readers know these books by.
TITLES = {3300: "The Wealth of Nations", 205: "Walden", 43: "Dr. Jekyll and Mr. Hyde",
          1080: "A Modest Proposal", 2591: "Grimms' Fairy Tales"}

# Genres a reader can browse by. A book is in a genre if one of its Gutenberg
# shelves is listed, or one of its subjects starts with a listed prefix.
GENRES = [
    ("Romance", {"Category: Romance"}, ("Love stories", "Man-woman relationships", "Courtship")),
    ("Mystery & crime", {"Category: Crime, Thrillers and Mystery", "Detective Fiction"},
     ("Detective and mystery", "Mystery fiction", "Mystery and detective")),
    ("Sci-fi & fantasy", {"Category: Science-Fiction & Fantasy", "Science Fiction"},
     ("Science fiction", "Fantasy fiction", "Fantasy literature")),
    ("Horror & ghost stories", {"Horror", "Gothic Fiction"}, ("Horror tales", "Ghost stories", "Gothic fiction")),
    ("Adventure", {"Category: Adventure", "Adventure"}, ("Adventure stories", "Sea stories")),
    ("Philosophy", {"Category: Philosophy & Ethics", "Philosophy"}, ("Philosophy", "Ethics")),
    ("Self-improvement", {"Category: How To ..."}, ("Conduct of life", "Success", "Self-culture", "Self-realization")),
    ("History", {"Category: History - Other", "Category: History - American", "Category: History - Modern (1750+)",
                 "Category: History - Early Modern (c. 1450-1750)", "Category: History - British",
                 "Category: History - Ancient", "Category: History - European", "Category: History - Warfare",
                 "Category: History - Religious", "US Civil War"}, ()),
    ("Biography & memoir", {"Category: Biographies"}, ("Autobiographies", "Biography")),
    ("Myths & legends", {"Category: Mythology, Legends & Folklore"}, ("Mythology", "Legends", "Fairy tales", "Folklore")),
    ("Poetry", {"Category: Poetry", "Poetry"}, ("Epic poetry",)),
    ("Plays", {"Category: Plays/Films/Dramas"}, ("Tragedies", "Comedies")),
    ("Funny books", {"Category: Humour"}, ("Humorous stories", "English wit and humor", "American wit and humor")),
    ("Children's classics", {"Category: Children & Young Adult Reading", "Children's Literature",
                             "Children's Picture Books"}, ()),
    ("Science & technology", {"Category: Science - Biology", "Category: Science - Physics",
                              "Category: Engineering & Technology", "Category: Science - Other",
                              "Category: Science - Earth/Agricultural/Farming"}, ("Evolution", "Natural selection")),
    ("Business & money", {"Category: Economics"}, ("Economics", "Business", "Wealth")),
    ("Politics & power", {"Category: Politics", "Politics"}, ("Political science",)),
    ("Spirituality", {"Category: Religion/Spirituality"}, ()),
    ("Travel", {"Category: Travel Writing"}, ("Voyages and travels",)),
    ("Short stories", {"Category: Short Stories"}, ("Short stories",)),
]
# Hand-picked lists Gutenberg keeps as shelves.
LISTS = [
    ("Banned books", "Banned Books from Anne Haight's list"),
    ("They became films", "Movie Books"),
    ("By Nobel Prize winners", "Nobel Prizes in Literature"),
    ("Bestsellers of 1895 to 1923", "Bestsellers, American, 1895-1923"),
]
# The home page, top to bottom. ("top", genre, n) is a ranked list; ("genre", name)
# and ("list", name) are plain rows; "week" and "long" come from word counts.
LAYOUT = [
    ("top", None, 10), ("week",), ("top", "Mystery & crime", 5), ("genre", "Romance"),
    ("genre", "Sci-fi & fantasy"), ("top", "Philosophy", 5), ("list", "Banned books"),
    ("genre", "Horror & ghost stories"), ("genre", "Adventure"), ("top", "Self-improvement", 5),
    ("list", "They became films"), ("genre", "Myths & legends"), ("top", "History", 5),
    ("list", "By Nobel Prize winners"), ("genre", "Funny books"), ("top", "Science & technology", 5),
    ("genre", "Poetry"), ("genre", "Plays"), ("genre", "Children's classics"), ("top", "Business & money", 5),
    ("genre", "Biography & memoir"), ("list", "Bestsellers of 1895 to 1923"), ("long",),
]


def short_title(title):
    """'Moby Dick; Or, The Whale' -> 'Moby Dick'."""
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


def genres_of(shelves, subjects):
    heads = [s.split(" -- ")[0] for s in subjects]
    return [name for name, shelf_set, prefixes in GENRES
            if shelf_set & set(shelves) or any(h.startswith(p) for h in heads for p in prefixes)]


def blurb(summary):
    """Gutenberg's summary, minus its 'X by Y is a novel' opener and its disclaimer. Returns (year, text)."""
    summary = re.sub(r"\s*\(This is an automatically generated summary\.\)", "", summary or "").strip()
    sentences = []
    for part in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'“])", summary):
        if sentences and re.search(r"\b(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|Capt|Col|Gen|Rev|Prof|Mt|[A-Z])\.$", sentences[-1]):
            sentences[-1] += " " + part  # 'Mr. Darcy' and 'H. G. Wells' don't end a sentence
        else:
            sentences.append(part)
    year = None
    if sentences and re.match(r"^[\"“].+?[\"”] by ", sentences[0]):
        m = re.search(r"\b(?:published|written|first appeared|composed)\b.*?\b(1[0-9]{3})\b", sentences[0])
        year = int(m.group(1)) if m else None
        sentences = sentences[1:]
    text = ""
    for s in sentences:
        if len(text) + len(s) > 320 and text:
            break
        text = f"{text} {s}".strip()
    return year, text


def word_count(book_id):
    path = TEXTS / str(book_id) / f"pg{book_id}.txt"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    start, end = text.find("*** START"), text.find("*** END")
    if start >= 0 and end > start:
        text = text[text.find("\n", start) + 1:end]
    return len(text.split())


def save_cover(book_id, force):
    """Copy the book's cover out of its HTML zip as a small WebP. Returns True if a cover exists."""
    out = COVERS / f"{book_id}.webp"
    if out.exists() and not force:
        return True
    try:
        from PIL import Image
    except ImportError:
        return False
    zpath = TEXTS / str(book_id) / f"pg{book_id}-h.zip"
    if not zpath.exists():
        return False
    with zipfile.ZipFile(zpath) as zf:
        # A scanned or volunteer-made cover ('images/cover.jpg', 'img-cover.jpg'). The plain covers
        # Gutenberg generates for books without one ('{id}-cover.png') are skipped: the page's
        # cloth covers look better.
        names = sorted((n for n in zf.namelist()
                        if re.search(r"cover[^/]*\.(jpe?g|png)$", n.rsplit("/", 1)[-1], re.I)
                        and not re.match(rf"{book_id}-cover\.png$", n)), key=len)
        if not names:
            return False
        image = Image.open(io.BytesIO(zf.read(names[0]))).convert("RGB")
    if image.width < 120:
        return False
    image.thumbnail((COVER_WIDTH, COVER_WIDTH * 2))
    COVERS.mkdir(parents=True, exist_ok=True)
    image.save(out, "WEBP", quality=72, method=6)
    return True


def load_catalog():
    catalog = {}
    if CATALOG.exists():
        for line in CATALOG.open(encoding="utf-8"):
            for b in json.loads(line).get("results", []):
                catalog.setdefault(b["id"], b)
    return catalog


def build(rows, catalog, force_covers=False):
    books, order = {}, []
    for r in rows:
        if r["check_translator"] == "yes":
            continue
        book_id = int(r["gutenberg_id"])
        entry = catalog.get(book_id, {})
        shelves, subjects = entry.get("bookshelves", []), entry.get("subjects", [])
        year, text = blurb((entry.get("summaries") or [""])[0])
        books[book_id] = {
            "title": TITLES.get(book_id) or short_title(r["title"]),
            "author": author_name(r["authors"]),
            "words": word_count(book_id),
            "year": year,
            "blurb": text,
            "genres": genres_of(shelves, subjects),
            "lists": [name for name, shelf in LISTS if shelf in shelves],
            "cover": save_cover(book_id, force_covers),
        }
        order.append(book_id)

    def ids(test, n=PER_ROW):
        return [i for i in order if test(books[i])][:n]

    out_rows = []
    for item in LAYOUT:
        kind = item[0]
        if kind == "top":
            genre, n = item[1], item[2]
            picked = ids(lambda b: genre is None or genre in b["genres"], n)
            name = f"Top {n} on Dripbook" if genre is None else f"Top {n} in {genre}"
            out_rows.append({"name": name, "ranked": True, "genre": genre, "books": picked})
        elif kind == "genre":
            out_rows.append({"name": item[1], "genre": item[1], "books": ids(lambda b: item[1] in b["genres"])})
        elif kind == "list":
            out_rows.append({"name": item[1], "books": ids(lambda b: item[1] in b["lists"])})
        elif kind == "week":
            out_rows.append({"name": "Finish it this week", "books": ids(
                lambda b: b["words"] and 2000 <= b["words"] <= 7 * WORDS_PER_MORNING)})
        elif kind == "long":
            longest = sorted((i for i in order[:400] if books[i]["words"]), key=lambda i: -books[i]["words"])
            out_rows.append({"name": "One big book, one morning at a time", "books": longest[:PER_ROW]})
    out_rows = [row for row in out_rows if len(row["books"]) >= 4]
    genres = [{"name": name, "count": sum(name in b["genres"] for b in books.values())} for name, *_ in GENRES]
    return {"total": len(order), "order": order, "books": {str(k): v for k, v in books.items()},
            "rows": out_rows, "genres": [g for g in genres if g["count"] >= 6]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force-covers", action="store_true", help="re-make cover images that already exist")
    args = ap.parse_args(argv)
    with open(BOOKS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    data = build(rows, load_catalog(), args.force_covers)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    books = data["books"].values()
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(books)} books, {len(data['rows'])} rows, "
          f"{sum(b['cover'] for b in books)} covers, {sum(b['words'] is not None for b in books)} word counts.")
    for row in data["rows"]:
        print(f"  {row['name']}: {len(row['books'])}")


if __name__ == "__main__":
    main()
