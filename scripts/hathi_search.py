#!/usr/bin/env python3
"""HathiTrust search for the website: our list of its public-domain books as static files the browser reads a
slice at a time (HTTP range requests), so the site needs no server or database for it.

  python3 scripts/sources.py hathi-index                       # once: HathiTrust's list (see sources.py)
  python3 scripts/hathi_search.py build                        # the files, in public-domain/cache/hathi-search
  python3 scripts/hathi_search.py search stoker dracula        # try them here
  uv run --with boto3 scripts/hathi_search.py upload           # to the R2 bucket named in .env.local

A search costs four round trips, each one batch of requests sent at once: top.json (once per visit), a dictionary
block per word, each word's postings, then the books' lines. About 20-200 KB in all.

Three logical files, each stored as numbered pieces of about 1 MB (dict-000.txt, postings-000.bin, rows-000.txt…).
Cloudflare copies a whole file into its cache the first time any part of it is asked for: 3-6 s for a 220 MB
file, 0.3 s for a piece. A record never spans two pieces. Offsets below are into the whole logical file;
top.json's "pieces" lists where each piece starts, and where the last one ends.

  top.json   {"v": version, "words": first word of each dictionary block, "at": its offset, "len": its size,
              "pieces": {"dict": [...], "postings": [...], "rows": [...]}}
  dict       'word<TAB>books<TAB>postings offset<TAB>postings size' lines, sorted, in blocks of about 4 KB
  postings   per word, its books' ids as varint gaps
  rows       'htid<TAB>bib<TAB>year<TAB>pd<TAB>title<TAB>author' per book, shortest titles first (before
             any ' / by…'; they're usually the book itself, not a collection). A book's id is where its line starts.

Each build is a new version folder, so a reader mid-search never mixes two builds; top.json says which is live.
site/home.js reads the same files (searchHathi) and must split words the same way (words() here).
"""
import argparse
import bisect
import csv
import datetime
import gzip
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "public-domain" / "cache" / "hathi_pd.tsv.gz"     # sources.py hathi-index
OUT = ROOT / "public-domain" / "cache" / "hathi-search"
PREFIX = "hathi/"                 # the folder in the bucket
PIECE = 1 << 20                   # bytes per piece, about
EXT = {"dict": "txt", "postings": "bin", "rows": "txt"}
TYPES = {"txt": "text/plain; charset=utf-8", "bin": "application/octet-stream"}
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


def piece_name(kind, i):
    return f"{kind}-{i:03d}.{EXT[kind]}"


class Pieces:
    """One logical file written as numbered pieces; a record starts a new piece rather than cross into one."""
    def __init__(self, folder, kind, size):
        self.folder, self.kind, self.size = folder, kind, size
        self.starts, self.at, self.f = [], 0, None

    def write(self, record):
        if self.f is None or (self.f.tell() and self.f.tell() + len(record) > self.size):
            if self.f:
                self.f.close()
            self.f = open(self.folder / piece_name(self.kind, len(self.starts)), "wb")
            self.starts.append(self.at)
        at = self.at
        self.f.write(record)
        self.at += len(record)
        return at

    def close(self):
        self.f.close()
        return self.starts + [self.at]


def build(src=SRC, out=OUT, version=None, block=4096, piece=PIECE):
    version = version or datetime.datetime.now().strftime("%Y%m%d-%H%M")
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
    rows_out = Pieces(folder, "rows", piece)
    for title, author, htid, bib, year, pd in books:
        at = rows_out.write(f"{htid}\t{bib}\t{year}\t{pd}\t{title}\t{author}\n".encode())
        for w in words(title + " " + author):
            post[w].append(at)
    top = {"v": version, "words": [], "at": [], "len": [], "pieces": {"rows": rows_out.close()}}
    post_out, dict_out = Pieces(folder, "postings", piece), Pieces(folder, "dict", piece)
    lines = []

    def end_block():
        b = b"".join(lines)
        top["at"].append(dict_out.write(b))
        top["len"].append(len(b))
        lines.clear()
    for w in sorted(post):
        b = varints(post[w])
        line = f"{w}\t{len(post[w])}\t{post_out.write(b)}\t{len(b)}\n".encode()
        if lines and sum(map(len, lines)) + len(line) > block:
            end_block()
        if not lines:
            top["words"].append(w)
        lines.append(line)
    end_block()
    top["pieces"].update(postings=post_out.close(), dict=dict_out.close())
    (Path(out) / "top.json").write_text(json.dumps(top, ensure_ascii=False, separators=(",", ":")))
    n = sum(len(p) - 1 for p in top["pieces"].values())
    print(f"{len(books):,} books, {len(post):,} words, {n} pieces -> {folder}", file=sys.stderr)
    return folder


def read(base, top, kind, at, size):
    """size bytes at an offset in a logical file, from the piece that holds them."""
    starts = top["pieces"][kind]
    i = bisect.bisect_right(starts, at) - 1
    with open(base / piece_name(kind, i), "rb") as f:
        f.seek(at - starts[i])
        return f.read(min(size, starts[i + 1] - at))


def search(q, folder=OUT, limit=30):
    """What the site's search does, on the files here: every word (but 'the', 'of'…) in the title or author."""
    top = json.loads((Path(folder) / "top.json").read_text())
    base = Path(folder) / top["v"]
    ws = words(q)
    ws = sorted(ws - STOP or ws)
    if not ws:
        return []
    lists = []
    for w in ws:
        i = bisect.bisect_right(top["words"], w) - 1
        if i < 0:
            return []
        block = read(base, top, "dict", top["at"][i], top["len"][i]).decode()
        line = next((ln for ln in block.split("\n") if ln.split("\t")[0] == w), None)
        if line is None:
            return []
        _, _, at, size = line.split("\t")
        lists.append(set(unvarints(read(base, top, "postings", int(at), int(size)))))
    out = []
    for at in sorted(set.intersection(*lists))[:limit]:
        line = read(base, top, "rows", at, ROW_BYTES).decode().split("\n")[0]
        htid, bib, year, pd, title, author = line.split("\t")
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


def upload(folder=OUT, switch=True):
    """The version's pieces (skipping any already there), then top.json, which makes it the live one."""
    import boto3                      # uv run --with boto3 …
    from botocore.config import Config
    e = env()
    top = json.loads((Path(folder) / "top.json").read_text())
    s3 = boto3.client("s3", endpoint_url=f"https://{e['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                      aws_access_key_id=e["R2_ACCESS_KEY_ID"], aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"],
                      region_name="auto", config=Config(request_checksum_calculation="when_required",
                                                        response_checksum_validation="when_required",
                                                        max_pool_connections=16))
    there = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=e["R2_BUCKET"], Prefix=f"{PREFIX}{top['v']}/"):
        there.update({o["Key"]: o["Size"] for o in page.get("Contents", [])})
    files = sorted((Path(folder) / top["v"]).iterdir())
    todo = [p for p in files if there.get(f"{PREFIX}{top['v']}/{p.name}") != p.stat().st_size]

    def send(p):
        s3.upload_file(str(p), e["R2_BUCKET"], f"{PREFIX}{top['v']}/{p.name}",
                       ExtraArgs={"ContentType": TYPES[p.suffix[1:]], "CacheControl": "public, max-age=31536000, immutable"})
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(send, todo))
    print(f"{PREFIX}{top['v']}/: sent {len(todo)} of {len(files)} pieces "
          f"({sum(p.stat().st_size for p in todo) / 1e6:.0f} MB)", file=sys.stderr)
    if not switch:
        print(f"Not live: {PREFIX}top.json still names the old version.", file=sys.stderr)
        return
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
    u = sub.add_parser("upload")
    u.add_argument("--hold", action="store_true", help="send the pieces but don't make them live yet")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        build()
    elif args.cmd == "search":
        for r in search(" ".join(args.words)):
            print(f"{r['id']:<32} {r['year']:<5} {r['title'][:60]} / {r['author'][:30]}")
    elif args.cmd == "upload":
        upload(switch=not args.hold)


if __name__ == "__main__":
    main()
