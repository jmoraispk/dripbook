"""Sign-up, confirmation and daily sending for Dripbook drips. Standard library only.

State lives in Upstash Redis (REST API), email goes through Postmark:
  sub:{token}           JSON subscription: email, book, pace, tz, status, sent, last_day, base
  who:{email}:{book}    token of that reader's subscription to that book
  active                set of tokens that get a part every morning
  blocks:{id}           a book as cut.py blocks (JSON, gzip, base64), read once from Gutenberg's mirror:
                        the HTML edition, or the plain text when the HTML is missing or garbled

A subscription is 'pending' until its link is confirmed (kept 7 days), then 'active'
until the last part ('done') or until Postmark says the reader unsubscribed ('stopped').
Parts are cut on the fly from the cached blocks (drip/cut.py is deterministic and fast).
"""
import base64
import gzip
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

import cut
import gutenberg_html

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
MIN_WORDS = 500          # below this the "book" is a stub (music, scans), not a drip
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_ZONE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$")   # an IANA name like Europe/Lisbon
# Book requests live in one Redis hash, one field per reader and title, so asking twice keeps one.
REQUESTS = "requests"
MAX_REQUESTS = 20_000    # a ceiling, so a flood of junk can't fill the database


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
        if n_plain and HTML_WORDS[0] <= n_rich / n_plain <= HTML_WORDS[1]:
            return rich
    return plain


def load_library():
    return json.loads(LIBRARY.read_text(encoding="utf-8"))["books"]


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --- The drip ---------------------------------------------------------------------------
class Drip:
    def __init__(self, redis, mail, env=None, fetch=fetch_gutenberg, library=None, clock=time.time, day=today,
                 fetch_html=fetch_gutenberg_html):
        self.redis, self.mail, self.fetch, self.fetch_html = redis, mail, fetch, fetch_html
        self.env = os.environ if env is None else env
        self.library = load_library() if library is None else library
        self.clock, self.day = clock, day
        self.broadcast = self.env.get("POSTMARK_BROADCAST_STREAM") or "broadcast-1"
        self._books = {}

    @classmethod
    def from_env(cls, mail=True):
        """From Vercel's env vars. mail=False for work that only touches the database."""
        env = os.environ
        url = env.get("KV_REST_API_URL") or env.get("UPSTASH_REDIS_REST_URL")
        token = env.get("KV_REST_API_TOKEN") or env.get("UPSTASH_REDIS_REST_TOKEN")
        key = env.get("POSTMARK_SERVER_TOKEN")
        if not (url and token and (key or not mail)):
            raise Problem(503, "Sign-up isn't set up on this server yet.")
        return cls(Redis(url, token), Postmark(key) if key else None)

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
        """{'title', 'author', 'blocks'} for a book, from the cache or Gutenberg's mirror."""
        if book_id in self._books:
            return self._books[book_id]
        info = self.library[book_id]
        raw = self.redis("GET", f"blocks:{book_id}")
        if raw:
            blocks = [tuple(b) for b in json.loads(gzip.decompress(base64.b64decode(raw)))]
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
            packed = gzip.compress(json.dumps(blocks, separators=(",", ":")).encode("utf-8"), 9)
            self.redis("SET", f"blocks:{book_id}", base64.b64encode(packed).decode())
        book = {"title": info["title"], "author": info.get("author", ""), "blocks": blocks}
        self._books[book_id] = book
        return book

    def parts(self, sub):
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
        if book_id not in self.library:
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
        self.redis("SADD", "active", sub["token"])
        return sub, self.send_next(sub)

    # Book requests
    def request_book(self, title, author, email, mine=False):
        """Save a request for a book we don't have yet. Asking again for the same title updates it."""
        title, author = " ".join(str(title or "").split()), " ".join(str(author or "").split())
        email = str(email or "").strip().lower()
        if not title:
            raise Problem(400, "Tell us the book's title.")
        if len(title) > 200 or len(author) > 200:
            raise Problem(400, "That's longer than any title we know. Shorten it a little.")
        if len(email) > 254 or not EMAIL.match(email):
            raise Problem(400, "That email address doesn't look right.")
        field = f"{email}|{title.lower()}"
        if not self.redis("HEXISTS", REQUESTS, field) and int(self.redis("HLEN", REQUESTS) or 0) >= MAX_REQUESTS:
            raise Problem(503, "We can't take requests right now. Try again tomorrow.")
        self.redis("HSET", REQUESTS, field, json.dumps({
            "title": title, "author": author, "email": email, "mine": bool(mine), "at": int(self.clock())}))
        if mine:
            return {"status": "saved", "message": f"Thanks. We'll write to {email} to set up {title} with you."}
        return {"status": "saved", "message": f"Thanks. We'll ask for {title} and email {email} when it's here."}

    # Sending
    def send_next(self, sub):
        """Email the reader's next part. Returns 'sent', 'done', 'stopped' or 'error'."""
        if sub["status"] != "active":
            return sub["status"]
        book, parts = self.book(sub["book"]), self.parts(sub)
        n = sub["sent"] + 1
        if n > len(parts):
            return self.finish(sub, "done")
        reply = self.mail(part_email(sub, book, parts, n, self.broadcast))
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
    inner = (f'<p style="{P}">You asked for <strong>{e(title)}</strong>{" by " + e(author) if author else ""}, '
             f'{sub["pace"]} minutes every morning. That\'s {n_parts} parts.</p>'
             f'<p style="margin:24px 0"><a href="{e(url)}" style="{BUTTON}">Yes, start my drip</a></p>'
             f'<p style="{SMALL}">Part 1 arrives as soon as you confirm, then one part each morning.</p>'
             f'<p style="{SMALL}">Didn\'t ask for this? Ignore this email and nothing happens.</p>')
    text = (f"You asked for {title}{' by ' + author if author else ''}, {sub['pace']} minutes every morning "
            f"({n_parts} parts).\n\nConfirm here to get part 1:\n{url}\n\n"
            "Didn't ask for this? Ignore this email and nothing happens.")
    return {"From": SENDER, "To": sub["email"], "Subject": f"Confirm your drip: {title}",
            "HtmlBody": page(inner), "TextBody": text, "MessageStream": "outbound", "Tag": "confirm"}


def part_email(sub, book, parts, n, stream):
    e = html.escape
    title, part, total = book["title"], parts[n - 1], len(parts)
    w = where(book, parts, n)
    if n < total:
        after_html = (f'<p style="{SMALL}">Part {n + 1} comes tomorrow morning. Can\'t wait? '
                      f'<a href="{e(link(sub, "next"))}" style="color:#666">Send me the next part now</a>.</p>')
        after_text = f"Part {n + 1} comes tomorrow morning. Can't wait? Send me the next part now:\n{link(sub, 'next')}"
    else:
        after_html = (f'<p style="{SMALL}">That\'s the end of {e(title)}. '
                      f'<a href="{e(sub["base"])}/" style="color:#666">Pick your next book</a>.</p>')
        after_text = f"That's the end of {title}. Pick your next book: {sub['base']}/"
    inner = (header_html(w)
             + cut.to_html(part, BODY)
             + '<hr style="border:0;border-top:1px solid #ddd;margin:32px 0 16px">'
             + after_html
             + f'<p style="{SMALL}">From the public-domain text. '
               f'<a href="{{{{{{ pm:unsubscribe }}}}}}" style="color:#666">Unsubscribe</a></p>')
    text = (f"{header_text(w)}\n\n{cut.to_text(part)}\n\n---\n{after_text}\n\n"
            "Unsubscribe: {{{ pm:unsubscribe }}}")
    return {"From": SENDER, "To": sub["email"], "Subject": f"{title}, part {n} of {total}",
            "HtmlBody": page(inner), "TextBody": text, "MessageStream": stream, "Tag": "part",
            "Metadata": {"book": sub["book"], "part": str(n)}}
