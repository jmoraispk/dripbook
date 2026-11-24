"""Sign-up, confirmation and daily sending for Dripbook drips. Standard library only.

State lives in Upstash Redis (REST API), email goes through Postmark:
  sub:{token}           JSON subscription: email, book, pace, tz, status, sent, last_day, base
  who:{email}:{book}    token of that reader's subscription to that book
  active                set of tokens that get a part every morning
  scan:{v}:ia:{id}      an Internet Archive scan read when a reader opened it: {title, author, year, blocks},
                        kept 90 days; scan-bad:{v}:ia:{id} says why one can't be read (7 days)
  hathi-twin:{v}:hathi:{id}  the Archive copy we read for a HathiTrust book (90 days); scan-ok:{v}:ia:{id}
                        marks a copy whose US rights HathiTrust vouched for
  blocks:{v}:{id}       a book as cut.py blocks (JSON, gzip, base64), read from Gutenberg's mirror:
                        the HTML edition, or the plain text when the HTML is missing or garbled.
                        v is READER: raise it when reading changes, so books are read again. Kept 90 days
  own:{id}              a reader's own PDF as the parts they cut in the editor (JSON, gzip, base64);
                        own-books tracks when each expires (30 days after its drip ends)
  own-pics:{id}         that book's pictures: part number -> JSON list of files in Vercel Blob
  own-upload:{id}       while the editor uploads the pictures: the key it was given (an hour)
  book-requests         list of book requests, one JSON entry each

A subscription is 'pending' until its link is confirmed (kept 7 days), then 'active'
until the last part ('done') or until Postmark says the reader unsubscribed ('stopped').
Parts are cut on the fly from the cached blocks (drip/cut.py is deterministic and fast).

Pictures in a reader's own PDF live in a private Vercel Blob store, one file per part (or a few
for a part with many pictures). They go inside each email as inline attachments, so they show
in the text, need nothing fetched when the reader opens the email, and stay in their inbox
after we delete the book. Blob deletes the files by itself: each is written with a timer
(ttl) that follows the book's: 8 days while unconfirmed, the drip's length plus 60 days once
it starts, and 31 days once it ends.
"""
import base64
import gzip
import hashlib
import html
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import cut
import gutenberg_html
import scan

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "site" / "library.json"
MIRROR = "https://gutenberg.pglaf.org/cache/epub/{id}/pg{id}.txt"
MIRROR_HTML = "https://gutenberg.pglaf.org/cache/epub/{id}/pg{id}-images.html"
# The HTML edition is used only if it has about as many words as the plain text; a few books
# keep their text in tables or images, or carry a big index the text leaves out.
HTML_WORDS = (0.85, 1.15)
POSTMARK_API = "https://api.postmarkapp.com/email"
SENDER = "Dripbook <lessons@mail.dripbook.ai>"
PENDING_SECONDS = 7 * 24 * 3600
RESEND_SECONDS = 60
READER = 2               # version of the cached blocks (see blocks:{v}:{id} above)
BLOCKS_DAYS = 90
MIN_WORDS = 500          # below this the "book" is a stub (music, scans), not a drip
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SCAN_ID = re.compile(r"^(ia|hathi):[A-Za-z0-9._$:/+-]{1,120}$")   # a scanned book picked from search
# An Internet Archive scan a reader can start at once: we read its OCR when they open it (Drip.scan).
SCAN_LIVE = re.compile(r"^ia:[A-Za-z0-9._-]{1,100}$")
# A HathiTrust book: read from the same scan on the Archive (Drip.hathi_twin).
HATHI_ID = re.compile(r"^hathi:[A-Za-z0-9._$:/+-]{1,120}$")
TWINS_TRIED = 4
MAX_SCAN_XML = 40_000_000         # the OCR file, uncompressed: ~450,000 words; 17 MB (200,000) reads in ~5 s
BAD_SCAN_SECONDS = 7 * 24 * 3600
TIME_ZONE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$")   # an IANA name like Europe/Lisbon
# Book requests: a Redis list with one entry per request. Only an identical request from the
# same address within 10 minutes (a double click) is dropped.
REQUESTS = "book-requests"
OLD_REQUESTS = "requests"   # the first version kept a hash; the export still reads it
MAX_REQUESTS = 20_000       # a ceiling, so a flood of junk can't fill the database
REPEAT_SECONDS = 600
# Drips of a reader's own PDF: the lessons they made in the cut editor (site/cuts.html), kept
# while the drip runs and for 30 days after it ends. Pending ones go when the sign-up expires.
OWN = "own:"
OWN_BOOKS = "own-books"           # sorted set: book key -> when it expires (inf while active)
OWN_KEEP_SECONDS = 30 * 24 * 3600
MAX_OWN_BOOKS = 200               # books stored at once: a ceiling for the free database
MAX_OWN_BYTES = 900_000           # compressed; Upstash's free plan takes requests up to 1 MB
MAX_OWN_LESSONS = 1000
OWN_KINDS = {"h2", "h3", "p", "quote", "verse", "note", "pg", "img"}
# Pictures: ("img", "i12 480") is picture i12, shown 480 px wide; its file comes with the part.
BLOB_API = "https://vercel.com/api/blob"
BLOB_API_VERSION = "13"           # what @vercel/blob 2.9 sends
OWN_PICS = "own-pics:"
OWN_UPLOAD = "own-upload:"
UPLOAD_SECONDS = 3600
MAX_PICTURE_BYTES = 25_000_000    # a book's pictures, all together
MAX_PART_PICTURE_BYTES = 7_000_000   # one email's; Postmark takes 10 MB with base64 and text
PICTURE_TYPES = {"image/jpeg": "jpg", "image/png": "png"}
PICTURE = re.compile(r"^(i\d{1,5}) (\d{1,4})$")
INLINE_TAG = re.compile(r"(</?(?:em|strong|sup|sub)>)")


class Problem(Exception):
    """A failure to show the reader: status code and a sentence."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# --- Services ---------------------------------------------------------------------------
class Redis:
    """Upstash Redis over its REST API: redis('SET', 'k', 'v') -> result."""
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def __call__(self, *args):
        req = urllib.request.Request(self.url, data=json.dumps([str(a) for a in args]).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                out = json.load(resp)
        except urllib.error.HTTPError as e:
            with e:
                out = json.load(e)
        if "error" in out:
            raise RuntimeError(f"Redis: {out['error']}")
        return out["result"]


class Postmark:
    def __init__(self, token):
        self.token = token

    def __call__(self, message):
        """Send one email; returns Postmark's reply (ErrorCode 0 means accepted)."""
        req = urllib.request.Request(POSTMARK_API, data=json.dumps(message).encode(), method="POST", headers={
            "Accept": "application/json", "Content-Type": "application/json", "X-Postmark-Server-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            with e:
                return json.load(e)


class Blob:
    """A private Vercel Blob store over its REST API: the same calls @vercel/blob makes."""
    def __init__(self, token):
        self.token = token

    def _api(self, path, data, headers, method="POST"):
        req = urllib.request.Request(BLOB_API + path, data=data, method=method, headers={
            "authorization": f"Bearer {self.token}", "x-api-version": BLOB_API_VERSION,
            "x-vercel-blob-store-id": self.token.split("_")[3] if self.token.count("_") >= 4 else "", **headers})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def put(self, pathname, data, content_type, ttl_days):
        """Store data at pathname, replacing what's there; returns the file's URL."""
        return self._api("/?" + urlencode({"pathname": pathname}), data, {
            "x-vercel-blob-access": "private", "x-content-type": content_type, "x-add-random-suffix": "0",
            "x-allow-overwrite": "1", "x-ttl-days": str(ttl_days)}, method="PUT")["url"]

    def get(self, url):
        if not (urlsplit(url).hostname or "").endswith(".blob.vercel-storage.com"):
            raise ValueError("not a Vercel Blob URL")   # never send the token anywhere else
        req = urllib.request.Request(url, headers={"authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def set_ttl(self, urls, days):
        """Delete these files `days` from now (1 to 730)."""
        self._api("/set-ttl", json.dumps({"urls": urls, "ttlDays": days}).encode(),
                  {"content-type": "application/json"})


def fetch_gutenberg(book_id, url=MIRROR):
    req = urllib.request.Request(url.format(id=book_id), headers={"User-Agent": "dripbook/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_gutenberg_html(book_id):
    return fetch_gutenberg(book_id, MIRROR_HTML)


def read_book(text, page=None):
    """A book's blocks: from its HTML edition (page) when that reads cleanly, else from the text."""
    plain = cut.paragraphs(cut.clean(text))
    if page:
        try:
            rich = gutenberg_html.blocks(page)
        except Exception as e:  # html.parser is forgiving; anything that still breaks it is a bad page
            print(f"HTML edition unreadable: {e!r}")
            rich = []
        n_plain = sum(cut.words(t) for _, t in plain)
        n_rich = sum(cut.words(t) for _, t in rich)
        # (a plain text that's only a cover pointing to the HTML, as for Haeckel's Evolution of Man)
        if n_plain and HTML_WORDS[0] <= n_rich / n_plain <= HTML_WORDS[1] or n_plain < MIN_WORDS <= n_rich:
            return cut.drop_editorial(rich)
    return cut.drop_editorial(plain)


def load_library():
    return json.loads(LIBRARY.read_text(encoding="utf-8"))["books"]


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --- The drip ---------------------------------------------------------------------------
class Drip:
    def __init__(self, redis, mail, env=None, fetch=fetch_gutenberg, library=None, clock=time.time, day=today,
                 fetch_html=fetch_gutenberg_html, blob=None, ia=scan):
        self.redis, self.mail, self.fetch, self.fetch_html, self.blob = redis, mail, fetch, fetch_html, blob
        self.ia = ia   # ia_files(identifier) and download_xml(identifier, info): scan.py, or a fake in tests
        self.env = os.environ if env is None else env
        self.library = load_library() if library is None else library
        self.clock, self.day = clock, day
        self.broadcast = self.env.get("POSTMARK_BROADCAST_STREAM") or "broadcast-1"
        self._books = {}
        self._scan_files = {}

    @classmethod
    def from_env(cls, mail=True):
        """From Vercel's env vars. mail=False for work that only touches the database."""
        env = os.environ
        url = env.get("KV_REST_API_URL") or env.get("UPSTASH_REDIS_REST_URL")
        token = env.get("KV_REST_API_TOKEN") or env.get("UPSTASH_REDIS_REST_TOKEN")
        key = env.get("POSTMARK_SERVER_TOKEN")
        if not (url and token and (key or not mail)):
            raise Problem(503, "Sign-up isn't set up on this server yet.")
        blob = env.get("BLOB_READ_WRITE_TOKEN")
        return cls(Redis(url, token), Postmark(key) if key else None, blob=Blob(blob) if blob else None)

    # Storage
    def get_sub(self, token):
        raw = self.redis("GET", f"sub:{token}") if token else None
        return json.loads(raw) if raw else None

    def save_sub(self, sub, expire=None):
        keys = (f"sub:{sub['token']}", f"who:{sub['email']}:{sub['book']}")
        values = (json.dumps(sub), sub["token"])
        for k, v in zip(keys, values):
            self.redis("SET", k, v, "EX", expire) if expire else self.redis("SET", k, v)

    def book(self, book_id):
        """{'title', 'author', 'blocks'} for a book, from the cache or Gutenberg's mirror.
        A reader's own PDF has 'parts' instead of 'blocks': the lessons they cut in the editor."""
        if book_id in self._books:
            return self._books[book_id]
        if book_id.startswith(OWN):
            raw = self.redis("GET", book_id)
            if not raw:
                raise Problem(404, "We don't have this book any more. Open it in the editor again to restart it.")
            data = unpack(raw)
            book = {"title": data["title"], "author": "", "own": True,
                    "parts": [[tuple(b) for b in part] for part in data["parts"]]}
            self._books[book_id] = book
            return book
        if SCAN_LIVE.match(book_id):
            return self.scan(book_id)
        info = self.library[book_id]
        raw = self.redis("GET", f"blocks:{READER}:{book_id}")
        if raw:
            blocks = [tuple(b) for b in unpack(raw)]
        else:
            try:
                text = self.fetch(book_id)
            except (OSError, ValueError) as e:
                raise Problem(502, "We couldn't load this book just now. Try again in a minute.") from e
            try:
                page = self.fetch_html(book_id) if self.fetch_html else None
            except (OSError, ValueError):
                page = None
            blocks = read_book(text, page)
            if sum(cut.words(t) for _, t in blocks) < MIN_WORDS:
                raise Problem(422, "This one isn't available as a drip. Try another book.")
            self.redis("SET", f"blocks:{READER}:{book_id}", pack(blocks), "EX", BLOCKS_DAYS * 86400)
        book = {"title": info["title"], "author": info.get("author", ""), "blocks": blocks}
        self._books[book_id] = book
        return book

    # Scans from the Internet Archive, read when a reader opens one
    def scan_files(self, scan_id):
        """The Archive's record of a scan, once we know we may send it and can read it; else a Problem
        whose message the site shows next to its Request button."""
        if not SCAN_LIVE.match(scan_id or ""):
            raise Problem(400, "We can't read that scan here. Request it instead.")
        if scan_id in self._scan_files:
            return self._scan_files[scan_id]
        bad = self.redis("GET", f"scan-bad:{READER}:{scan_id}")
        if bad:
            raise Problem(422, bad)
        try:
            info = self.ia.ia_files(scan_id[3:])
        except (OSError, ValueError, KeyError) as e:
            raise Problem(502, "The Internet Archive didn't answer. Try again in a minute.") from e
        if not scan.us_public_domain(info) and not self.redis("GET", f"scan-ok:{READER}:{scan_id}"):
            self.refuse(scan_id, "This scan may still be in copyright, so we can't send it yet. Request it and we'll check.")
        if not info.get("xml"):
            self.refuse(scan_id, "This scan has no text we can read. Request it and we'll look for another copy.")
        if info.get("xml_size", 0) > MAX_SCAN_XML:
            self.refuse(scan_id, "This scan is too long for us to read here. Request it and we'll prepare it.")
        self._scan_files[scan_id] = info
        return info

    def refuse(self, scan_id, message):
        self.redis("SET", f"scan-bad:{READER}:{scan_id}", message, "EX", BAD_SCAN_SECONDS)
        raise Problem(422, message)

    def scan(self, scan_id):
        """{'title', 'author', 'year', 'blocks'} for an Internet Archive scan: from the cache, or its OCR read
        and cleaned now (a few seconds)."""
        if scan_id in self._books:
            return self._books[scan_id]
        raw = self.redis("GET", f"scan:{READER}:{scan_id}") if SCAN_LIVE.match(scan_id or "") else None
        if raw:
            book = unpack(raw)
        else:
            info = self.scan_files(scan_id)
            try:
                xml = self.ia.download_xml(scan_id[3:], info, timeout=40)
            except (OSError, ValueError) as e:
                raise Problem(502, "We couldn't download this scan just now. Try again in a minute.") from e
            try:
                blocks = scan.scan_blocks(scan.scan_pages(xml))
            except scan.ET.ParseError:
                self.refuse(scan_id, "This scan's text is damaged. Request it and we'll look for another copy.")
            if sum(cut.words(t) for _, t in blocks) < MIN_WORDS:
                self.refuse(scan_id, "This scan has too little text to make a drip. Request it and we'll take a look.")
            if not scan.readable(blocks):
                self.refuse(scan_id, "This scan's text came out too garbled to read well. Request it and we'll clean it by hand.")
            book = {"title": scan.short_title(info["title"]) or scan_id[3:], "author": scan.short_author(info["author"]),
                    "year": info.get("year", ""), "blocks": blocks}
            packed = pack(book)
            if len(packed) > MAX_OWN_BYTES:
                self.refuse(scan_id, "This scan is too long for us to keep. Request it and we'll prepare it.")
            self.redis("SET", f"scan:{READER}:{scan_id}", packed, "EX", BLOCKS_DAYS * 86400)
        book = {**book, "scan": True, "blocks": [tuple(b) for b in book["blocks"]]}
        self._books[scan_id] = book
        return book

    def hathi_twin(self, scan_id):
        """'ia:<identifier>' for a HathiTrust book: the same scan on the Archive, which we can read. HathiTrust's
        catalog tells us the book's US rights and OCLC numbers; when it says public domain and the Archive copy has
        the same OCLC number or year, its word stands for the copy too (scan-ok), even if the Archive's doesn't."""
        if not HATHI_ID.match(scan_id or ""):
            raise Problem(400, "We can't read that scan here. Request it instead.")
        key = f"hathi-twin:{READER}:{scan_id}"
        twin = self.redis("GET", key)
        if twin:
            return twin
        bad = self.redis("GET", f"scan-bad:{READER}:{scan_id}")
        if bad:
            raise Problem(422, bad)
        try:
            rec = self.ia.hathi_record(scan_id[6:])
            twins = self.ia.ia_twins(rec)
        except (OSError, ValueError, KeyError) as e:
            raise Problem(502, "HathiTrust or the Internet Archive didn't answer. Try again in a minute.") from e
        if rec["rights"] not in scan.HATHI_PD:
            self.refuse(scan_id, "HathiTrust doesn't list this one as public domain in the US, so we can't send it yet. "
                                 "Request it and we'll check.")
        down = None
        for t in twins[:TWINS_TRIED]:
            if t["oclc"] or t["year"] == rec["year"]:
                self.redis("SET", f"scan-ok:{READER}:{t['id']}", scan_id)
            try:
                self.scan_files(t["id"])
            except Problem as e:
                down = down or (e if e.status == 502 else None)
                continue
            self.redis("SET", key, t["id"], "EX", BLOCKS_DAYS * 86400)
            return t["id"]
        if down:
            raise down
        self.refuse(scan_id, "We couldn't find a copy of this scan that we can read. Request it and we'll look further.")

    def scan_find(self, scan_id):
        """The first, quick step of opening a scan: its preview if we've read it before, else its record and the
        size of its OCR file, so the site can tell how long reading it will take. A HathiTrust book answers with
        its Archive copy's id, which the site reads and starts."""
        if HATHI_ID.match(scan_id or ""):
            return {**self.scan_find(self.hathi_twin(scan_id)), "via": scan_id}
        if SCAN_LIVE.match(scan_id or "") and self.redis("GET", f"scan:{READER}:{scan_id}"):
            return {"ready": True, **self.scan_preview(scan_id)}
        info = self.scan_files(scan_id)
        return {"ready": False, "id": scan_id, "title": scan.short_title(info["title"]),
                "author": scan.short_author(info["author"]), "year": info.get("year", ""), "size": info.get("xml_size", 0)}

    def scan_preview(self, scan_id):
        """What the site shows before a reader starts a scan: how long it is at each pace, and part 1."""
        if HATHI_ID.match(scan_id or ""):
            scan_id = self.hathi_twin(scan_id)
        book = self.scan(scan_id)
        parts = {p: cut.cut_blocks(book["blocks"], p) for p in cut.PACES}
        first = parts[10][0]
        return {"id": scan_id, "title": book["title"], "author": book["author"], "year": book["year"],
                "words": sum(cut.words(t) for _, t in book["blocks"]),
                "parts": {str(p): len(v) for p, v in parts.items()},
                "first": {"heading": cut.plain(cut.part_heading(first) or ""), "html": cut.to_html(first)}}

    def parts(self, sub):
        book = self.book(sub["book"])
        if "parts" in book:
            return book["parts"]
        return cut.cut_blocks(self.book(sub["book"])["blocks"], sub["pace"], sub["book"] not in cut.KEEP_FRONT_MATTER)

    # Sign-up
    def allowed(self, email):
        """ALLOWED_EMAILS ('me@x.com, @x.com') limits sign-up to a few people before launch."""
        rule = [a.strip().lower() for a in self.env.get("ALLOWED_EMAILS", "").split(",") if a.strip()]
        return not rule or any(email == a or (a.startswith("@") and email.endswith(a)) for a in rule)

    def subscribe(self, email, book_id, pace, base, tz=""):
        email = str(email or "").strip().lower()
        book_id = str(book_id or "").strip()
        if len(email) > 254 or not EMAIL.match(email):
            raise Problem(400, "That email address doesn't look right.")
        if book_id not in self.library and not SCAN_LIVE.match(book_id):
            raise Problem(400, "We don't have that book.")
        try:
            pace = int(pace)
        except (TypeError, ValueError):
            pace = 0
        if pace not in cut.PACES:
            raise Problem(400, "Pick 5, 10 or 20 minutes a day.")
        if not self.allowed(email):
            raise Problem(403, "Dripbook isn't open to everyone yet. Sign-up opens soon.")

        book = self.book(book_id)
        old = self.get_sub(self.redis("GET", f"who:{email}:{book_id}"))
        if old and old["status"] == "active":
            return {"status": "already", "message": f"You're already reading {book['title']}. "
                    "Your next part comes tomorrow morning."}
        if old and old["status"] == "pending" and self.clock() - old.get("asked", 0) < RESEND_SECONDS:
            raise Problem(429, "We just sent you an email. Check your inbox, and your spam folder.")

        sub = old if old and old["status"] == "pending" else {
            "token": secrets.token_urlsafe(24), "email": email, "book": book_id,
            "status": "pending", "sent": 0, "last_day": "", "created": int(self.clock())}
        tz = str(tz or "")
        sub.update(pace=pace, base=base, asked=int(self.clock()),
                   tz=tz if len(tz) <= 64 and TIME_ZONE.match(tz) else "")
        self.save_sub(sub, expire=PENDING_SECONDS)
        reply = self.mail(confirm_email(sub, book, len(self.parts(sub))))
        if reply.get("ErrorCode", 1) != 0:
            raise Problem(502, "We couldn't send the confirmation email. Try again in a minute.")
        return {"status": "sent", "message": f"Almost there. We sent a link to {email}: "
                "click it to get part 1."}

    def confirm(self, token):
        sub = self.get_sub(token)
        if not sub:
            raise Problem(404, "This link has expired. Pick the book again on dripbook.ai to get a new one.")
        if sub["status"] != "pending":
            return sub, "already"
        sub["status"] = "active"
        sub["confirmed"] = int(self.clock())
        self.save_sub(sub)
        if sub["book"].startswith(OWN):
            self.keep_own(sub["book"], n_parts=len(self.parts(sub)))
        self.redis("SADD", "active", sub["token"])
        return sub, self.send_next(sub)

    # Book requests
    def request_book(self, title, author, email, mine=False, sender="", gutenberg=None, source=None):
        """Save a request for a book we don't have yet. Every request is kept, in order.
        gutenberg is the book's Gutenberg number when it was picked from search (site/catalog.json);
        source names a scan picked from search: 'ia:<Internet Archive identifier>' or 'hathi:<HathiTrust id>'."""
        title, author = " ".join(str(title or "").split()), " ".join(str(author or "").split())
        gutenberg = str(gutenberg or "").strip()
        if gutenberg and (not gutenberg.isdigit() or len(gutenberg) > 7):
            raise Problem(400, "We don't know that book. Search for it again.")
        source = str(source or "").strip()
        if source and not SCAN_ID.match(source):
            raise Problem(400, "We don't know that book. Search for it again.")
        email = str(email or "").strip().lower()
        if not title:
            raise Problem(400, "Tell us the book's title.")
        if len(title) > 200 or len(author) > 200:
            raise Problem(400, "That's longer than any title we know. Shorten it a little.")
        if len(email) > 254 or not EMAIL.match(email):
            raise Problem(400, "That email address doesn't look right.")
        if mine:
            reply = {"status": "saved", "message": f"Thanks. We'll write to {email} to set up {title} with you."}
        elif gutenberg or source:
            reply = {"status": "saved", "message": f"Requested. We'll email {email} when {title} is ready to start."}
        else:
            reply = {"status": "saved", "message": f"Thanks. We'll ask for {title} and email {email} when it's here."}
        same = hashlib.sha256(json.dumps([sender, title, author, email, bool(mine), gutenberg, source]).encode()).hexdigest()[:32]
        if not self.redis("SET", f"request-seen:{same}", "1", "EX", REPEAT_SECONDS, "NX"):
            return reply   # the same request again, moments later
        if int(self.redis("LLEN", REQUESTS) or 0) >= MAX_REQUESTS:
            raise Problem(503, "We can't take requests right now. Try again tomorrow.")
        entry = {"title": title, "author": author, "email": email, "mine": bool(mine), "at": int(self.clock())}
        if gutenberg:
            entry["gutenberg"] = int(gutenberg)
        if source:
            entry["source"] = source
        self.redis("RPUSH", REQUESTS, json.dumps(entry))
        return reply

    # A reader's own PDF
    def subscribe_own(self, email, title, lessons, base, tz="", rights=False):
        """Start a drip of lessons cut in the editor. Same confirmation and mornings as a library book.

        When the lessons have pictures, no email goes yet: the reply is {'status': 'upload', 'book',
        'key'}, the editor sends each part's pictures to add_pictures(), then calls pictures_done()."""
        email = str(email or "").strip().lower()
        if len(email) > 254 or not EMAIL.match(email):
            raise Problem(400, "That email address doesn't look right.")
        if not rights:
            raise Problem(400, "Tick the box to confirm you may use this PDF.")
        if not self.allowed(email):
            raise Problem(403, "Dripbook isn't open to everyone yet. Sign-up opens soon.")
        title = " ".join(str(title or "").split())[:200] or "Your book"
        parts = clean_lessons(lessons, pictures=self.blob is not None)
        packed = pack({"title": title, "parts": parts})
        if len(packed) > MAX_OWN_BYTES:
            raise Problem(413, "This book is too long for us to send yet. Try a shorter PDF, or skip parts you don't need.")
        if not self.redis("SET", f"own-asked:{email}", "1", "EX", RESEND_SECONDS, "NX"):
            raise Problem(429, "We just sent you an email. Check your inbox, and your spam folder.")
        now = int(self.clock())
        self.redis("ZREMRANGEBYSCORE", OWN_BOOKS, "-inf", now)
        if int(self.redis("ZCARD", OWN_BOOKS) or 0) >= MAX_OWN_BOOKS:
            raise Problem(503, "We can't take more PDFs right now. Try again tomorrow.")
        book_id = OWN + secrets.token_urlsafe(12)
        self.redis("SET", book_id, packed, "EX", PENDING_SECONDS)
        self.redis("ZADD", OWN_BOOKS, now + PENDING_SECONDS, book_id)
        tz = str(tz or "")
        sub = {"token": secrets.token_urlsafe(24), "email": email, "book": book_id, "title": title,
               "status": "pending", "sent": 0, "last_day": "", "created": now, "pace": 0, "base": base,
               "asked": now, "tz": tz if len(tz) <= 64 and TIME_ZONE.match(tz) else ""}
        self.save_sub(sub, expire=PENDING_SECONDS)
        if any(k == "img" for part in parts for k, _ in part):
            key = secrets.token_urlsafe(24)
            self.redis("SET", OWN_UPLOAD + book_id[len(OWN):], json.dumps({"key": key, "token": sub["token"], "bytes": 0}),
                       "EX", UPLOAD_SECONDS)
            return {"status": "upload", "book": book_id, "key": key}
        return self.ask_own(sub)

    def ask_own(self, sub):
        """The confirmation email for a reader's own book."""
        reply = self.mail(confirm_email(sub, self.book(sub["book"]), len(self.parts(sub))))
        if reply.get("ErrorCode", 1) != 0:
            raise Problem(502, "We couldn't send the confirmation email. Try again in a minute.")
        return {"status": "sent", "message": f"Almost there. We sent a link to {sub['email']}: click it to get lesson 1."}

    def upload(self, book_id, key):
        """The upload record for this book, if key is the one subscribe_own gave out."""
        book_id = str(book_id or "")
        raw = self.redis("GET", OWN_UPLOAD + book_id[len(OWN):]) if book_id.startswith(OWN) else None
        record = json.loads(raw) if raw else None
        if not record or not secrets.compare_digest(record["key"], str(key or "")):
            raise Problem(403, "This upload has expired. Start again from the editor.")
        return book_id, record

    def add_pictures(self, book_id, key, part, images):
        """Store one part's pictures, {name: {'type', 'data' (base64)}}, as one file in Blob."""
        book_id, record = self.upload(book_id, key)
        parts = self.book(book_id)["parts"]
        try:
            part = int(part)
        except (TypeError, ValueError):
            part = 0
        if not 1 <= part <= len(parts) or not isinstance(images, dict) or not images:
            raise Problem(400, "These pictures didn't come through. Try again.")
        wanted = {PICTURE.match(t).group(1) for k, t in parts[part - 1] if k == "img"}
        size, clean = 0, {}
        for name, image in images.items():
            if name not in wanted or not isinstance(image, dict) or image.get("type") not in PICTURE_TYPES:
                raise Problem(400, "These pictures didn't come through. Try again.")
            try:
                size += len(base64.b64decode(str(image.get("data", "")), validate=True))
            except ValueError:
                raise Problem(400, "These pictures didn't come through. Try again.")
            clean[name] = {"type": image["type"], "data": image["data"]}
        sizes = record.setdefault("parts", {})
        if record["bytes"] + size > MAX_PICTURE_BYTES or sizes.get(str(part), 0) + size > MAX_PART_PICTURE_BYTES:
            raise Problem(413, "These pictures are too big for us to send. They'll show their captions instead.")
        pics = OWN_PICS + book_id[len(OWN):]
        files = json.loads(self.redis("HGET", pics, part) or "[]")
        try:
            url = self.blob.put(f"own/{book_id[len(OWN):]}/{part}-{len(files)}.json",
                                json.dumps(clean, separators=(",", ":")).encode(), "application/json",
                                ttl_days=PENDING_SECONDS // 86400 + 1)
        except (OSError, ValueError, KeyError) as e:
            print(f"Blob put failed: {e!r}")
            raise Problem(502, "We couldn't save these pictures just now.")
        self.redis("HSET", pics, part, json.dumps(files + [url]))
        self.redis("EXPIRE", pics, PENDING_SECONDS)
        record["bytes"] += size
        sizes[str(part)] = sizes.get(str(part), 0) + size
        self.redis("SET", OWN_UPLOAD + book_id[len(OWN):], json.dumps(record), "EX", UPLOAD_SECONDS)
        return {"status": "saved"}

    def pictures_done(self, book_id, key):
        """Every picture is in (or the editor gave up on some): send the confirmation email."""
        book_id, record = self.upload(book_id, key)
        sub = self.get_sub(record["token"])
        if not sub:
            raise Problem(403, "This upload has expired. Start again from the editor.")
        out = self.ask_own(sub)
        self.redis("DEL", OWN_UPLOAD + book_id[len(OWN):])
        return out

    def picture_files(self, book_id, part=None):
        """The Blob URLs of a book's pictures: one part's, or all of them."""
        pics = OWN_PICS + book_id[len(OWN):]
        if part is not None:
            return json.loads(self.redis("HGET", pics, part) or "[]")
        return [u for v in (self.redis("HGETALL", pics) or [])[1::2] for u in json.loads(v)]

    def pictures(self, book_id, part):
        """{name: (content type, base64)} for one part, read from Blob; {} if they can't be read."""
        out = {}
        if not self.blob:
            return out
        for url in self.picture_files(book_id, part):
            try:
                for name, image in json.loads(self.blob.get(url)).items():
                    out[name] = (image["type"], image["data"])
            except (OSError, ValueError, KeyError) as e:
                print(f"Blob get failed for part {part}: {e!r}")   # the email goes without them
        return out

    def keep_own(self, book_id, seconds=None, n_parts=0):
        """Keep a reader's own book, and its pictures, while their drip runs, or for `seconds` more."""
        if not book_id.startswith(OWN):
            return
        pics = OWN_PICS + book_id[len(OWN):]
        if seconds is None:
            self.redis("PERSIST", book_id)
            self.redis("PERSIST", pics)
            self.redis("ZADD", OWN_BOOKS, "+inf", book_id)
            days = min(730, n_parts + 60)   # one part a day at the slowest, with room to spare
        else:
            self.redis("EXPIRE", book_id, seconds)
            self.redis("EXPIRE", pics, seconds)
            self.redis("ZADD", OWN_BOOKS, int(self.clock()) + seconds, book_id)
            days = seconds // 86400 + 1
        files = self.picture_files(book_id) if self.blob else []
        if files:
            try:
                self.blob.set_ttl(files, days)
            except (OSError, ValueError) as e:
                print(f"Blob set-ttl failed for {book_id}: {e!r}")

    # Sending
    def send_next(self, sub):
        """Email the reader's next part. Returns 'sent', 'done', 'stopped' or 'error'."""
        if sub["status"] != "active":
            return sub["status"]
        book, parts = self.book(sub["book"]), self.parts(sub)
        n = sub["sent"] + 1
        if n > len(parts):
            return self.finish(sub, "done")
        pictures = (self.pictures(sub["book"], n) if book.get("own") and any(k == "img" for k, _ in parts[n - 1])
                    else {})
        reply = self.mail(part_email(sub, book, parts, n, self.broadcast, pictures))
        code = reply.get("ErrorCode", 1)
        if code == 406:   # Postmark: the reader unsubscribed, bounced or complained
            return self.finish(sub, "stopped")
        if code != 0:
            print(f"Postmark error {code} for {sub['token'][:6]}: {reply.get('Message')}")
            return "error"
        sub["sent"], sub["last_day"] = n, self.day()
        if n == len(parts):
            return self.finish(sub, "done", sent=True)
        self.save_sub(sub)
        return "sent"

    def finish(self, sub, status, sent=False):
        sub["status"] = status
        self.save_sub(sub)
        self.redis("SREM", "active", sub["token"])
        self.keep_own(sub["book"], OWN_KEEP_SECONDS)
        return "sent" if sent else status

    def send_now(self, token):
        sub = self.get_sub(token)
        if not sub:
            raise Problem(404, "We can't find this drip any more.")
        return sub, self.send_next(sub)

    def run_daily(self):
        """The morning job: one part to every active reader who hasn't had one today."""
        tally = {}
        for token in sorted(self.redis("SMEMBERS", "active") or []):
            sub = self.get_sub(token)
            if not sub:
                self.redis("SREM", "active", token)
                outcome = "missing"
            elif sub.get("last_day") == self.day():
                outcome = "skipped"
            else:
                try:
                    outcome = self.send_next(sub)
                except Problem as e:
                    print(f"{token[:6]}: {e.message}")
                    outcome = "error"
            tally[outcome] = tally.get(outcome, 0) + 1
        return tally


def pack(value):
    """JSON, gzipped and base64'd: how books are kept in Redis."""
    return base64.b64encode(gzip.compress(json.dumps(value, separators=(",", ":")).encode("utf-8"), 9)).decode()


def unpack(raw):
    return json.loads(gzip.decompress(base64.b64decode(raw)))


def safe_inline(text):
    """Html from the browser, cut down to text and <em>, <strong>, <sup> and <sub>."""
    pieces = INLINE_TAG.split(text)
    out = "".join(p if INLINE_TAG.fullmatch(p) else html.escape(html.unescape(p), quote=False) for p in pieces)
    return gutenberg_html.close_tags(out)


def clean_lessons(lessons, pictures=True):
    """The editor's lessons, [[[kind, html], ...], ...], checked and made safe for an email.
    Pictures are dropped (their captions stay) when there's nowhere to keep them."""
    bad = Problem(400, "These lessons didn't come through. Reload the editor and try again.")
    if not isinstance(lessons, list) or not lessons:
        raise bad
    if len(lessons) > MAX_OWN_LESSONS:
        raise Problem(400, f"That's more than {MAX_OWN_LESSONS} lessons. Merge some and try again.")
    parts, seen = [], set()
    for lesson in lessons:
        if not isinstance(lesson, list):
            raise bad
        part = []
        for block in lesson:
            if not (isinstance(block, list) and len(block) == 2 and all(isinstance(x, str) for x in block)):
                raise bad
            kind, text = block
            kind = kind if kind in OWN_KINDS else "p"
            if kind == "img":
                m = PICTURE.match(text)
                if not (pictures and m and 0 < int(m.group(2)) and m.group(1) not in seen):
                    continue
                seen.add(m.group(1))
            elif kind == "pg":
                text = re.sub(r"[^0-9A-Za-z]", "", text)[:12]
            else:
                text = safe_inline(text).strip()
            if text:
                part.append((kind, text))
        if any(k != "pg" for k, _ in part):
            parts.append(part)
    if not parts:
        raise Problem(400, "We couldn't find any text in these lessons.")
    return parts


# --- Emails -----------------------------------------------------------------------------
FONT = "font-family:Georgia,'Times New Roman',serif"
SANS = "font-family:Helvetica,Arial,sans-serif"
P = f"margin:0 0 1em;{FONT};font-size:18px;line-height:1.6;color:#222"
H = f"margin:1.6em 0 .8em;{FONT};font-size:22px;line-height:1.3;color:#111"
SMALL = f"margin:0 0 1em;{SANS};font-size:14px;line-height:1.5;color:#666"
BUTTON = ("display:inline-block;padding:12px 22px;border-radius:999px;background:#F2B230;color:#1a1300;"
          f"{SANS};font-weight:bold;text-decoration:none")
BODY = {
    "p": P,
    "h2": H,
    "h3": f"margin:1.5em 0 .7em;{FONT};font-size:19px;line-height:1.35;font-weight:bold;color:#111",
    "blockquote": "margin:0 0 1em;padding:.15em 0 .15em 18px;border-left:3px solid #E9D9A8",
    "quote": f"margin:.4em 0;{FONT};font-size:17px;line-height:1.55;color:#333",
    "verse": f"margin:0 0 1em 1.4em;{FONT};font-size:18px;line-height:1.55;color:#222",
    "note": f"margin:0 0 1em;{FONT};font-size:15px;line-height:1.5;color:#555",
}
ROMAN = re.compile(r"^[IVXLC]+[.:]?$")
SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor", "of", "on", "or",
               "the", "to", "with"}


def tidy(heading, limit=80):
    """A heading for the email's header line: ALL CAPS softened to title case, long ones cut."""
    letters = [c for c in heading if c.isalpha()]
    if len(letters) > 3 and all(c.isupper() for c in letters):
        words = heading.split()
        heading = " ".join(w if ROMAN.match(w) else w.lower() if i and w.lower() in SMALL_WORDS
                           else w[:1] + w[1:].lower() for i, w in enumerate(words))
    return heading if len(heading) <= limit else heading[:limit - 1].rsplit(" ", 1)[0] + "…"


def where(book, parts, n):
    """The lines that open a part's email: book and part, chapter and section, pages and time."""
    ctx = cut.context(parts, n, book["title"])
    total = len(parts)
    minutes = max(1, round(cut.part_words(parts[n - 1]) / cut.WORDS_PER_MINUTE))
    last_page = next((t for part in reversed(parts) for k, t in reversed(part) if k == "pg"), None)
    if ctx["pages"]:
        first, last = ctx["pages"]
        pages = f"Page {first}" if first == last else f"Pages {first}–{last}"
        if last_page and last_page.isdigit():
            pages += f" of {last_page}"
    else:
        done = sum(cut.part_words(x) for x in parts[:n - 1])
        pages = f"{round(100 * done / max(1, done + sum(cut.part_words(x) for x in parts[n - 1:])))}% through"
    return {"top": f"{book['title']} · Part {n} of {total}",
            "trail": [tidy(t) for t in ctx["trail"]],
            "meta": f"{pages} · {minutes} min read",
            "progress": n / total}


def header_html(w):
    e = html.escape
    trail = " <span style=\"color:#B89A4E\">›</span> ".join(e(t) for t in w["trail"])
    filled = max(2, round(100 * w["progress"]))
    bar = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:10px 0 0">'
           f'<tr><td width="{filled}%" style="height:4px;background:#F2B230;font-size:0;line-height:0">&nbsp;</td>'
           + (f'<td style="height:4px;background:#F1EBDA;font-size:0;line-height:0">&nbsp;</td>' if filled < 100 else "")
           + "</tr></table>")
    return (f'<p style="margin:0;{SANS};font-size:13px;line-height:1.5;color:#8A8577">{e(w["top"])}</p>'
            + (f'<p style="margin:6px 0 0;{FONT};font-size:17px;line-height:1.4;color:#1a1a1a">{trail}</p>'
               if trail else "")
            + f'<p style="margin:6px 0 0;{SANS};font-size:13px;line-height:1.5;color:#8A8577">{e(w["meta"])}</p>'
            + bar + '<div style="height:28px;line-height:28px;font-size:0">&nbsp;</div>')


def header_text(w):
    return "\n".join([w["top"]] + ([" › ".join(w["trail"])] if w["trail"] else []) + [w["meta"]])


def page(inner):
    return ('<!doctype html><html><body style="margin:0;padding:24px 16px;background:#fff">'
            f'<div style="max-width:600px;margin:0 auto">{inner}</div></body></html>')


def link(sub, action):
    return f"{sub['base']}/api/{action}?t={sub['token']}"


def confirm_email(sub, book, n_parts):
    e = html.escape
    title, author = book["title"], book["author"]
    url = link(sub, "confirm")
    if book.get("own"):
        asked_html = f'You asked for <strong>{e(title)}</strong>, from the PDF you cut into {n_parts} parts'
        asked_text = f"You asked for {title}, from the PDF you cut into {n_parts} parts"
    else:
        by = " by " + author if author else ""
        asked_html = (f'You asked for <strong>{e(title)}</strong>{e(by)}, {sub["pace"]} minutes every morning. '
                      f'That\'s {n_parts} parts')
        asked_text = f"You asked for {title}{by}, {sub['pace']} minutes every morning ({n_parts} parts)"
    inner = (f'<p style="{P}">{asked_html}.</p>'
             f'<p style="margin:24px 0"><a href="{e(url)}" style="{BUTTON}">Yes, start my drip</a></p>'
             f'<p style="{SMALL}">Part 1 arrives as soon as you confirm, then one part each morning.</p>'
             f'<p style="{SMALL}">Didn\'t ask for this? Ignore this email and nothing happens'
             + (', and we delete the book in 7 days.' if book.get("own") else '.') + '</p>')
    text = (f"{asked_text}.\n\nConfirm here to get part 1:\n{url}\n\n"
            "Didn't ask for this? Ignore this email and nothing happens.")
    return {"From": SENDER, "To": sub["email"], "Subject": f"Confirm your drip: {title}",
            "HtmlBody": page(inner), "TextBody": text, "MessageStream": "outbound", "Tag": "confirm",
            "TrackOpens": False, "TrackLinks": "None"}


def picture_html(spec, pictures):
    """An inline picture: <img> pointing at the attachment with the same Content-ID."""
    m = PICTURE.match(spec)
    if not m or m.group(1) not in pictures:
        return ""
    name, width = m.group(1), min(int(m.group(2)), 560)
    cid = f"{name}.{PICTURE_TYPES[pictures[name][0]]}"
    return (f'<p style="margin:0 0 1em;text-align:center"><img src="cid:{cid}" width="{width}" alt="" '
            f'style="display:block;margin:0 auto;width:100%;max-width:{width}px;height:auto;border:0"></p>')


def part_email(sub, book, parts, n, stream, pictures=None):
    """One part. pictures, {name: (type, base64)}, go in as inline attachments."""
    pictures = pictures or {}
    e = html.escape
    title, part, total = book["title"], parts[n - 1], len(parts)
    w = where(book, parts, n)
    source = ("From the PDF you sent us. We delete it 30 days after the last part." if book.get("own")
              else "From the public-domain text.")
    if n < total:
        after_html = (f'<p style="{SMALL}">Part {n + 1} comes tomorrow morning. Can\'t wait? '
                      f'<a href="{e(link(sub, "next"))}" style="color:#666">Send me the next part now</a>.</p>')
        after_text = f"Part {n + 1} comes tomorrow morning. Can't wait? Send me the next part now:\n{link(sub, 'next')}"
    else:
        after_html = (f'<p style="{SMALL}">That\'s the end of {e(title)}. '
                      f'<a href="{e(sub["base"])}/" style="color:#666">Pick your next book</a>.</p>')
        after_text = f"That's the end of {title}. Pick your next book: {sub['base']}/"
    inner = (header_html(w)
             + cut.to_html(part, {**BODY, "img": lambda spec: picture_html(spec, pictures)})
             + '<hr style="border:0;border-top:1px solid #ddd;margin:32px 0 16px">'
             + after_html
             + f'<p style="{SMALL}">{source} '
               f'<a href="{{{{{{ pm:unsubscribe }}}}}}" style="color:#666">Unsubscribe</a></p>')
    text = (f"{header_text(w)}\n\n{cut.to_text(part)}\n\n---\n{after_text}\n\n"
            "Unsubscribe: {{{ pm:unsubscribe }}}")
    message = {"From": SENDER, "To": sub["email"], "Subject": f"{title}, part {n} of {total}",
               "HtmlBody": page(inner), "TextBody": text, "MessageStream": stream, "Tag": "part",
               "TrackOpens": False, "TrackLinks": "None",
               "Metadata": {"book": sub["book"], "part": str(n)}}
    shown = [PICTURE.match(t).group(1) for k, t in part if k == "img" and PICTURE.match(t)]
    attachments = [{"Name": f"{name}.{PICTURE_TYPES[pictures[name][0]]}", "Content": pictures[name][1],
                    "ContentType": pictures[name][0], "ContentID": f"cid:{name}.{PICTURE_TYPES[pictures[name][0]]}"}
                   for name in shown if name in pictures]
    if attachments:
        message["Attachments"] = attachments
    return message
