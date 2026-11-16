"""Sign-up, confirmation and daily sending for Dripbook drips. Standard library only.

State lives in Upstash Redis (REST API), email goes through Postmark:
  sub:{token}           JSON subscription: email, book, pace, status, sent, last_day, base
  who:{email}:{book}    token of that reader's subscription to that book
  active                set of tokens that get a part every morning
  book:{id}             the clean text of a book (gzip, base64), fetched once from Gutenberg's mirror

A subscription is 'pending' until its link is confirmed (kept 7 days), then 'active'
until the last part ('done') or until Postmark says the reader unsubscribed ('stopped').
Parts are cut on the fly from the cached text (drip/cut.py is deterministic and fast).
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

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "site" / "library.json"
MIRROR = "https://gutenberg.pglaf.org/cache/epub/{id}/pg{id}.txt"
POSTMARK_API = "https://api.postmarkapp.com/email"
SENDER = "Dripbook <lessons@mail.dripbook.ai>"
PENDING_SECONDS = 7 * 24 * 3600
RESEND_SECONDS = 60
MIN_WORDS = 500          # below this the "book" is a stub (music, scans), not a drip
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def fetch_gutenberg(book_id):
    req = urllib.request.Request(MIRROR.format(id=book_id), headers={"User-Agent": "dripbook/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_library():
    return json.loads(LIBRARY.read_text(encoding="utf-8"))["books"]


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --- The drip ---------------------------------------------------------------------------
class Drip:
    def __init__(self, redis, mail, env=None, fetch=fetch_gutenberg, library=None, clock=time.time, day=today):
        self.redis, self.mail, self.fetch = redis, mail, fetch
        self.env = os.environ if env is None else env
        self.library = load_library() if library is None else library
        self.clock, self.day = clock, day
        self.broadcast = self.env.get("POSTMARK_BROADCAST_STREAM") or "broadcast-1"
        self._books = {}

    @classmethod
    def from_env(cls):
        env = os.environ
        url = env.get("KV_REST_API_URL") or env.get("UPSTASH_REDIS_REST_URL")
        token = env.get("KV_REST_API_TOKEN") or env.get("UPSTASH_REDIS_REST_TOKEN")
        mail = env.get("POSTMARK_SERVER_TOKEN")
        if not (url and token and mail):
            raise Problem(503, "Sign-up isn't set up on this server yet.")
        return cls(Redis(url, token), Postmark(mail))

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
        """{'title', 'author', 'text'} for a book, from the cache or Gutenberg's mirror."""
        if book_id in self._books:
            return self._books[book_id]
        info = self.library[book_id]
        raw = self.redis("GET", f"book:{book_id}")
        if raw:
            text = gzip.decompress(base64.b64decode(raw)).decode("utf-8")
        else:
            try:
                text = cut.clean(self.fetch(book_id))
            except (OSError, ValueError) as e:
                raise Problem(502, "We couldn't load this book just now. Try again in a minute.") from e
            if cut.words(text) < MIN_WORDS:
                raise Problem(422, "This one isn't available as a drip. Try another book.")
            self.redis("SET", f"book:{book_id}", base64.b64encode(gzip.compress(text.encode("utf-8"))).decode())
        book = {"title": info["title"], "author": info.get("author", ""), "text": text}
        self._books[book_id] = book
        return book

    def parts(self, sub):
        return cut.cut(self.book(sub["book"])["text"], sub["pace"])

    # Sign-up
    def allowed(self, email):
        """ALLOWED_EMAILS ('me@x.com, @x.com') limits sign-up to a few people before launch."""
        rule = [a.strip().lower() for a in self.env.get("ALLOWED_EMAILS", "").split(",") if a.strip()]
        return not rule or any(email == a or (a.startswith("@") and email.endswith(a)) for a in rule)

    def subscribe(self, email, book_id, pace, base):
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
        sub.update(pace=pace, base=base, asked=int(self.clock()))
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
P = f"margin:0 0 1em;{FONT};font-size:18px;line-height:1.6;color:#222"
H = f"margin:1.6em 0 .8em;{FONT};font-size:22px;line-height:1.3;color:#111"
SMALL = "margin:0 0 1em;font-family:Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;color:#666"
BUTTON = ("display:inline-block;padding:12px 22px;border-radius:999px;background:#F2B230;color:#1a1300;"
          "font-family:Helvetica,Arial,sans-serif;font-weight:bold;text-decoration:none")


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
    minutes = max(1, round(cut.part_words(part) / cut.WORDS_PER_MINUTE))
    head = f"{title} · Part {n} of {total} · {minutes} min"
    if n < total:
        after_html = (f'<p style="{SMALL}">Part {n + 1} comes tomorrow morning. Can\'t wait? '
                      f'<a href="{e(link(sub, "next"))}" style="color:#666">Send me the next part now</a>.</p>')
        after_text = f"Part {n + 1} comes tomorrow morning. Can't wait? Send me the next part now:\n{link(sub, 'next')}"
    else:
        after_html = (f'<p style="{SMALL}">That\'s the end of {e(title)}. '
                      f'<a href="{e(sub["base"])}/" style="color:#666">Pick your next book</a>.</p>')
        after_text = f"That's the end of {title}. Pick your next book: {sub['base']}/"
    inner = (f'<p style="{SMALL}">{e(head)}</p>'
             + cut.to_html(part, P, H)
             + '<hr style="border:0;border-top:1px solid #ddd;margin:32px 0 16px">'
             + after_html
             + f'<p style="{SMALL}">From the public-domain text. '
               f'<a href="{{{{{{ pm:unsubscribe }}}}}}" style="color:#666">Unsubscribe</a></p>')
    text = (f"{head}\n\n{cut.to_text(part)}\n\n---\n{after_text}\n\n"
            "Unsubscribe: {{{ pm:unsubscribe }}}")
    return {"From": SENDER, "To": sub["email"], "Subject": f"{title}, part {n} of {total}",
            "HtmlBody": page(inner), "TextBody": text, "MessageStream": stream, "Tag": "part",
            "Metadata": {"book": sub["book"], "part": str(n)}}

