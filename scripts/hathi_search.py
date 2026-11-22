#!/usr/bin/env python3
"""HathiTrust search for the website: our list of its public-domain books as static files the browser reads a
slice at a time (HTTP range requests), so the site needs no server or database for it.

  python3 scripts/sources.py hathi-index                       # once: HathiTrust's list (see sources.py)
  python3 scripts/hathi_search.py build                        # the files, in public-domain/cache/hathi-search
  python3 scripts/hathi_search.py search stoker dracula        # try them here
  uv run --with boto3 scripts/hathi_search.py upload           # to the R2 bucket named in .env.local

A search costs four round trips, each one batch of requests sent at once: top.json (once per visit), a dictionary
block per word, each word's postings, then the books' lines. About 20-200 KB in all.

  top.json      {"v": version, "words": first word of each dictionary block, "at": its byte offset, "len": its size}
  <v>/dict.txt  'word<TAB>books<TAB>postings offset<TAB>postings size' lines, sorted, in blocks of about 4 KB
  <v>/postings  per word, its books' ids as varint gaps
  <v>/rows.txt  'htid<TAB>bib<TAB>year<TAB>pd<TAB>title<TAB>author' per book, shortest titles first (before
                any ' / by…'; they're usually the book itself, not a collection). A book's id is where its line starts.

Each build is a new version folder, so a reader mid-search never mixes two builds; top.json says which is live.
site/home.js reads the same files (searchHathi) and must split words the same way (words() here).
"""
import argparse
import csv
import datetime
import gzip
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "public-domain" / "cache" / "hathi_pd.tsv.gz"     # sources.py hathi-index
OUT = ROOT / "public-domain" / "cache" / "hathi-search"
PREFIX = "hathi/"                 # the folder in the bucket
FILES = {"dict.txt": "text/plain; charset=utf-8", "postings": "application/octet-stream",
         "rows.txt": "text/plain; charset=utf-8"}
STOP = set("a an and the of in on by to for with or at from as is".split())
ROW_BYTES = 1500                  # a line is at most this long (300-character title, 150-character author)


def words(s):
    """Lowercase words without accents: 'Émile, ou De l'éducation' -> {'emile', 'ou', 'de', 'l', 'education'}."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    return set(re.findall(r"[^\W_]+", s))


def varints(ids):
    out, prev = bytearray(), 0
    for i in ids:
        d, prev = i - prev, i
        while d >= 128:
            out.append(d & 127 | 128)
            d >>= 7
        out.append(d)
    return bytes(out)


def unvarints(b):
    ids, n, d, shift = [], 0, 0, 0
    for x in b:
        d |= (x & 127) << shift
        if x & 128:
            shift += 7
        else:
            n += d
            ids.append(n)
            d = shift = 0
    return ids


def build(src=SRC, out=OUT, version=None, block=4096):
    version = version or datetime.date.today().isoformat()
    folder = Path(out) / version
    folder.mkdir(parents=True, exist_ok=True)
    clean = lambda s, n: re.sub(r"\s+", " ", s).strip()[:n]
    csv.field_size_limit(1 << 24)
    books = []
    with gzip.open(src, "rt", encoding="utf-8", newline="") as f:
        rows = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        col = {k: i for i, k in enumerate(next(rows))}
        for x in rows:
            if len(x) == len(col) and x[col["title"]].strip():
                books.append((clean(x[col["title"]], 300), clean(x[col["author"]], 150), x[col["htid"]],
                              x[col["bib"]], x[col["year"]][:4], int(x[col["rights"]] == "pd")))
    books.sort(key=lambda b: len(b[0].split(" / ")[0]))
    post = defaultdict(list)
    with open(folder / "rows.txt", "wb") as f:
        for title, author, htid, bib, year, pd in books:
            at = f.tell()
            f.write(f"{htid}\t{bib}\t{year}\t{pd}\t{title}\t{author}\n".encode())
            for w in words(title + " " + author):
                post[w].append(at)
    top = {"v": version, "words": [], "at": [], "len": []}
    with open(folder / "postings", "wb") as pf, open(folder / "dict.txt", "wb") as df:
        start = 0
        for w in sorted(post):
            b = varints(post[w])
            line = f"{w}\t{len(post[w])}\t{pf.tell()}\t{len(b)}\n".encode()
            pf.write(b)
            if not top["words"] or df.tell() + len(line) - start > block:
                if top["words"]:
                    top["len"].append(df.tell() - start)
                start = df.tell()
                top["words"].append(w)
                top["at"].append(start)
            df.write(line)
        top["len"].append(df.tell() - start)
    (Path(out) / "top.json").write_text(json.dumps(top, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(books):,} books, {len(post):,} words -> {folder}", file=sys.stderr)
    return folder


def search(q, folder=OUT, limit=30):
    """What the site's search does, on the files here: every word (but 'the', 'of'…) in the title or author."""
    top = json.loads((Path(folder) / "top.json").read_text())
    base = Path(folder) / top["v"]
    ws = words(q)
    ws = sorted(ws - STOP or ws)
    if not ws:
        return []
    lists = []
    with open(base / "dict.txt", "rb") as df, open(base / "postings", "rb") as pf:
        for w in ws:
            i = max((n for n, first in enumerate(top["words"]) if first <= w), default=None)
            if i is None:
                return []
            df.seek(top["at"][i])
            line = next((ln for ln in df.read(top["len"][i]).decode().split("\n") if ln.split("\t")[0] == w), None)
            if line is None:
                return []
            _, _, at, size = line.split("\t")
            pf.seek(int(at))
            lists.append(set(unvarints(pf.read(int(size)))))
    out = []
    with open(base / "rows.txt", "rb") as rf:
        for at in sorted(set.intersection(*lists))[:limit]:
            rf.seek(at)
            htid, bib, year, pd, title, author = rf.readline().decode().rstrip("\n").split("\t")
            out.append({"id": "hathi:" + htid, "title": title, "author": author, "year": year,
                        "url": f"https://catalog.hathitrust.org/Record/{bib}"})
    return out


def env():
    """R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET: from the environment or .env.local."""
    vals = {}
    path = ROOT / ".env.local"
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r'\s*([A-Z0-9_]+)\s*=\s*"?(.*?)"?\s*$', line)
            if m:
                vals[m[1]] = m[2]
    vals.update({k: v for k, v in os.environ.items() if k.startswith("R2_")})
    missing = [k for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET") if not vals.get(k)]
    if missing:
        sys.exit("Missing " + ", ".join(missing) + " (add them to .env.local)")
    return vals


def upload(folder=OUT):
    import boto3                      # uv run --with boto3 …
    from botocore.config import Config
    e = env()
    top = json.loads((Path(folder) / "top.json").read_text())
    s3 = boto3.client("s3", endpoint_url=f"https://{e['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                      aws_access_key_id=e["R2_ACCESS_KEY_ID"], aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"],
                      region_name="auto", config=Config(request_checksum_calculation="when_required",
                                                        response_checksum_validation="when_required"))
    for name, kind in FILES.items():
        key = f"{PREFIX}{top['v']}/{name}"
        print(f"{key} ({(Path(folder) / top['v'] / name).stat().st_size / 1e6:.0f} MB)…", file=sys.stderr)
        s3.upload_file(str(Path(folder) / top["v"] / name), e["R2_BUCKET"], key,
                       ExtraArgs={"ContentType": kind, "CacheControl": "public, max-age=31536000, immutable"})
    # last, so readers switch only once the whole version is there
    s3.upload_file(str(Path(folder) / "top.json"), e["R2_BUCKET"], PREFIX + "top.json",
                   ExtraArgs={"ContentType": "application/json", "CacheControl": "public, max-age=3600"})
    print(f"Live: {PREFIX}top.json -> version {top['v']}. Older version folders can be deleted a day later.",
          file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    s = sub.add_parser("search")
    s.add_argument("words", nargs="+")
    sub.add_parser("upload")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        build()
    elif args.cmd == "search":
        for r in search(" ".join(args.words)):
            print(f"{r['id']:<32} {r['year']:<5} {r['title'][:60]} / {r['author'][:30]}")
    elif args.cmd == "upload":
        upload()


if __name__ == "__main__":
    main()
