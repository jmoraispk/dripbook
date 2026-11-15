#!/usr/bin/env python3
"""Download the books in books.csv from Project Gutenberg: HTML zip and plain text.

Standard library only.

  python3 public-domain/download.py                  # all books, both formats
  python3 public-domain/download.py --limit 5        # trial run: first 5 books
  python3 public-domain/download.py --formats txt    # only the plain text

Files go to public-domain/books/{id}/ as pg{id}-h.zip (HTML with images) and
pg{id}.txt (UTF-8 text). Anything already there is skipped, so the script can
be stopped with Ctrl-C and run again to resume. Misses are listed, with a
reason, in books/failed.csv; a later run retries them and drops the ones it gets.

Gutenberg's robot policy forbids automated downloads from www.gutenberg.org and
points to its mirrors, so this reads from gutenberg.pglaf.org, the mirror run by
Gutenberg itself. It makes one request at a time, with a pause between them.
"""
import argparse
import csv
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOOKS_CSV = HERE / "books.csv"
OUT = HERE / "books"
MIRROR = "https://gutenberg.pglaf.org/cache/epub"
USER_AGENT = "dripbook-catalog/0.1"
FILENAMES = {"html": "pg{id}-h.zip", "txt": "pg{id}.txt"}
TRIES = 4


def read_ids(path=BOOKS_CSV):
    with open(path, newline="", encoding="utf-8") as f:
        return [row["gutenberg_id"].strip() for row in csv.DictReader(f)]


def check_zip(path):
    """Raise ValueError unless path is a readable zip with no corrupt members."""
    try:
        with zipfile.ZipFile(path) as z:
            if not z.namelist():
                raise ValueError("zip is empty")
            bad = z.testzip()
            if bad:
                raise ValueError(f"corrupt file in zip: {bad}")
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a valid zip: {e}")


def fetch(url, dest, tries=TRIES):
    """Download url to dest (via dest.part). Retries transient errors; returns "" or a reason."""
    part = dest.with_name(dest.name + ".part")
    reason = ""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as out:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
            if dest.suffix == ".zip":
                check_zip(part)
            elif part.stat().st_size == 0:
                raise ValueError("empty file")
            part.replace(dest)
            return ""
        except urllib.error.HTTPError as e:
            reason = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                break  # 404 and friends won't change on retry
        except (OSError, ValueError) as e:  # network trouble, truncated or bad file
            reason = str(e) or type(e).__name__
        if attempt < tries - 1:
            time.sleep(5 * (attempt + 1))
    part.unlink(missing_ok=True)
    return reason


def load_failed(path):
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {(r["gutenberg_id"], r["format"]): r["reason"] for r in csv.DictReader(f)}


def save_failed(path, failed):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["gutenberg_id", "format", "reason"])
        for (book_id, fmt), reason in sorted(failed.items(), key=lambda kv: (int(kv[0][0]), kv[0][1])):
            w.writerow([book_id, fmt, reason])


def log_line(message):
    print(message, flush=True)  # flush so progress shows when output goes to a file


def download_all(ids, formats, out=OUT, pause=1.0, log=log_line):
    """Fetch every missing file. Returns (downloaded, skipped, failed_count)."""
    out.mkdir(parents=True, exist_ok=True)
    failed_path = out / "failed.csv"
    failed = load_failed(failed_path)
    downloaded = skipped = 0
    first_request = True
    try:
        for n, book_id in enumerate(ids, 1):
            folder = out / book_id
            for fmt in formats:
                name = FILENAMES[fmt].format(id=book_id)
                dest = folder / name
                if dest.exists():
                    skipped += 1
                    failed.pop((book_id, fmt), None)
                    continue
                folder.mkdir(exist_ok=True)
                if not first_request:
                    time.sleep(pause)
                first_request = False
                reason = fetch(f"{MIRROR}/{book_id}/{name}", dest)
                if reason:
                    failed[(book_id, fmt)] = reason
                    save_failed(failed_path, failed)
                    log(f"[{n}/{len(ids)}] {name}: FAILED ({reason})")
                else:
                    failed.pop((book_id, fmt), None)
                    downloaded += 1
                    log(f"[{n}/{len(ids)}] {name}: ok ({dest.stat().st_size:,} bytes)")
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()  # every format failed: leave no empty folder
    finally:
        save_failed(failed_path, failed)
    return downloaded, skipped, len(failed)


def folder_size(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only the first N books in books.csv")
    ap.add_argument("--formats", default="html,txt", help="comma-separated: html, txt (default both)")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between requests (default 1)")
    args = ap.parse_args(argv)

    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    unknown = [f for f in formats if f not in FILENAMES]
    if unknown or not formats:
        ap.error(f"--formats must be from: {', '.join(FILENAMES)}")

    ids = read_ids()
    if args.limit:
        ids = ids[:args.limit]
    try:
        downloaded, skipped, failed = download_all(ids, formats, pause=args.pause)
    except KeyboardInterrupt:
        print("\nStopped. Run again to resume.", file=sys.stderr)
        return 130
    gb = folder_size(OUT) / 1e9
    print(f"Done: {downloaded} downloaded, {skipped} already there, {failed} failed "
          f"(see books/failed.csv). books/ is {gb:.2f} GB.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
