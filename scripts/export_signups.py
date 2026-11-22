"""Export drip sign-ups and book requests from the Upstash database to two CSV files.

  python3 scripts/export_signups.py            # writes exports/subscribers.csv and exports/requests.csv
  python3 scripts/export_signups.py --out DIR

It needs KV_REST_API_URL and KV_REST_API_TOKEN, from the shell or from .env.local, which
`vercel env pull .env.local` writes (or copy them from the database's page in Upstash).
exports/ and .env* are git-ignored: these files hold readers' email addresses.

Unconfirmed sign-ups are deleted after 7 days, so the subscriber list only has the recent ones.
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "drip"))
from app import OLD_REQUESTS, REQUESTS, Redis, load_library  # noqa: E402

# book is a library id, or own:… for a reader's own PDF.
SUB_FIELDS = ["email", "book", "title", "status", "pace", "sent", "tz", "created", "confirmed", "last_day"]
REQUEST_FIELDS = ["at", "title", "author", "email", "mine", "gutenberg_id", "scan"]   # picked from search: a Gutenberg number, or ia:/hathi: id


def load_env(path):
    """KEY=value lines from a dotenv file, without overriding the shell."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def when(seconds):
    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime("%Y-%m-%d %H:%M") if seconds else ""


def subscribers(redis, library):
    """Every sub:* record, oldest first."""
    keys, cursor = [], "0"
    while True:
        cursor, batch = redis("SCAN", cursor, "MATCH", "sub:*", "COUNT", 500)
        keys += batch
        if str(cursor) == "0":
            break
    rows = []
    for i in range(0, len(keys), 100):
        for raw in redis("MGET", *keys[i:i + 100]):
            if raw:
                sub = json.loads(raw)
                rows.append({**{k: sub.get(k, "") for k in SUB_FIELDS},
                             "title": sub.get("title") or library.get(sub.get("book"), {}).get("title", ""),
                             "created": when(sub.get("created")), "confirmed": when(sub.get("confirmed"))})
    return sorted(rows, key=lambda r: r["created"])


def requests(redis):
    """Every book request, oldest first."""
    rows = [json.loads(v) for v in redis("LRANGE", REQUESTS, 0, -1) or []]
    rows += [json.loads(v) for v in (redis("HGETALL", OLD_REQUESTS) or [])[1::2]]   # from before 22 Nov 2026
    rows.sort(key=lambda r: r.get("at", 0))
    return [{**{k: r.get(k, "") for k in REQUEST_FIELDS}, "at": when(r.get("at")),
             "mine": "yes" if r.get("mine") else "", "gutenberg_id": r.get("gutenberg", ""), "scan": r.get("source", "")} for r in rows]


def write(path, fields, rows):
    with path.open("w", newline="") as f:
        out = csv.DictWriter(f, fieldnames=fields)
        out.writeheader()
        out.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "exports")
    args = ap.parse_args(argv)
    load_env(ROOT / ".env.local")
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not (url and token):
        sys.exit("Set KV_REST_API_URL and KV_REST_API_TOKEN, or run: vercel env pull .env.local")
    redis = Redis(url, token)
    subs, reqs = subscribers(redis, load_library()), requests(redis)
    args.out.mkdir(parents=True, exist_ok=True)
    write(args.out / "subscribers.csv", SUB_FIELDS, subs)
    write(args.out / "requests.csv", REQUEST_FIELDS, reqs)

    status = Counter(s["status"] for s in subs)
    print(f"{len(subs)} sign-ups ({', '.join(f'{n} {k}' for k, n in status.most_common()) or 'none'}), "
          f"{len({s['email'] for s in subs})} people -> {args.out / 'subscribers.csv'}")
    print(f"{len(reqs)} book requests -> {args.out / 'requests.csv'}")
    for title, n in Counter(r["title"].lower() for r in reqs).most_common(5):
        print(f"  {n} x {title}")


if __name__ == "__main__":
    main()
