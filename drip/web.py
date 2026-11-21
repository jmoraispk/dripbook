"""HTTP side of the drip: the Vercel Python functions in api/ are thin wrappers around these.

Each endpoint takes a Request and returns (status, content_type, body). Links in emails
open a page that POSTs, so mail scanners that only follow links can't confirm a sign-up or
send a part. The page posts itself as soon as it opens in a browser, so for the reader it's
one click; without JavaScript it shows the button.
"""
import html
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, urlsplit

from app import Drip, Problem

JSON = "application/json"
HTML = "text/html; charset=utf-8"


class Request:
    def __init__(self, method, path, headers, body=b""):
        url = urlsplit(path)
        self.method, self.headers = method, {k.lower(): v for k, v in headers.items()}
        self.query = dict(parse_qsl(url.query))
        self.body = body

    def data(self):
        """The body as a dict, from JSON or a form."""
        if not self.body:
            return {}
        if self.headers.get("content-type", "").startswith("application/json"):
            try:
                out = json.loads(self.body)
            except ValueError:
                raise Problem(400, "Bad request.")
            return out if isinstance(out, dict) else {}
        return dict(parse_qsl(self.body.decode("utf-8", errors="replace")))

    def base(self):
        """Where links in emails point: SITE_URL, or the host this request came to."""
        site = os.environ.get("SITE_URL")
        if site:
            return site.rstrip("/")
        host = self.headers.get("x-forwarded-host") or self.headers.get("host", "localhost")
        proto = self.headers.get("x-forwarded-proto") or ("http" if host.startswith("localhost") else "https")
        return f"{proto}://{host}"


def page(title, text, button=None, token="", auto=False):
    """A small page in the site's look. button posts the token back to the same URL;
    with auto, the page posts it by itself as soon as it opens."""
    form = (f'<form method="post" id="go"><input type="hidden" name="t" value="{html.escape(token)}">'
            f'<button class="btn" type="submit">{html.escape(button)}</button></form>') if button else ""
    if form and auto:
        form += '<script>document.getElementById("go").submit()</script>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)} · Dripbook</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400..700&family=Literata:opsz,wght@7..72,400..700&display=swap">
<link rel="stylesheet" href="/home.css">
</head><body class="page">
<main style="max-width:560px;margin:14vh auto 0;padding:0 16px">
<p><a href="/" style="color:var(--muted);text-decoration:none">Dripbook</a></p>
<h1 style="font-family:var(--serif, Georgia, serif);font-size:2rem;line-height:1.2;margin:.3em 0">{html.escape(title)}</h1>
<p style="color:var(--muted);margin:0 0 24px">{html.escape(text)}</p>
{form}
</main></body></html>"""


def guarded(fn, kind):
    """Turn a Problem (or a crash) into a reply the reader can understand."""
    def run(req):
        try:
            return fn(req)
        except Problem as e:
            status, message = e.status, e.message
        except Exception:
            traceback.print_exc()
            status, message = 500, "Something went wrong on our side. Try again in a minute."
        if kind == JSON:
            return status, JSON, json.dumps({"error": message})
        return status, HTML, page("That didn't work", message)
    return run


def drip(mail=True):
    return Drip.from_env(mail)


# --- Endpoints --------------------------------------------------------------------------
def subscribe(req):
    data = req.data()
    out = drip().subscribe(data.get("email"), data.get("book"), data.get("pace"), req.base(), data.get("tz"))
    return 200, JSON, json.dumps(out)


def request_book(req):
    data = req.data()
    if data.get("website"):   # a field people never see: only bots fill it in
        return 200, JSON, json.dumps({"status": "saved", "message": "Thanks."})
    mine = str(data.get("mine", "")).lower() in ("1", "true", "on", "yes")
    sender = req.headers.get("x-forwarded-for", "").split(",")[0].strip()
    out = drip(mail=False).request_book(data.get("title"), data.get("author"), data.get("email"), mine, sender)
    return 200, JSON, json.dumps(out)


def subscribe_own(req):
    data = req.data()
    out = drip().subscribe_own(data.get("email"), data.get("title"), data.get("lessons"), req.base(),
                               data.get("tz"), data.get("rights") is True)
    return 200, JSON, json.dumps(out)


def pictures(req):
    """The editor sends a part's pictures, {book, key, part, images}, then {book, key, done: true}."""
    data = req.data()
    d = drip()
    if data.get("done") is True:
        out = d.pictures_done(data.get("book"), data.get("key"))
    else:
        out = d.add_pictures(data.get("book"), data.get("key"), data.get("part"), data.get("images"))
    return 200, JSON, json.dumps(out)


def confirm_get(req):
    return 200, HTML, page("Starting your drip", "One moment. If nothing happens, press the button.",
                           "Start my drip", req.query.get("t", ""), auto=True)


def confirm_post(req):
    d = drip()
    sub, outcome = d.confirm(req.data().get("t") or req.query.get("t"))
    title = d.book(sub["book"])["title"]
    if outcome == "already":
        return 200, HTML, page("You're already in", f"{title} keeps coming every morning.")
    if outcome != "sent":
        raise Problem(502, "You're confirmed, but part 1 didn't go out. It'll come with tomorrow's email.")
    return 200, HTML, page("You're in", f"Part 1 of {title} is on its way to {sub['email']}. "
                           "A new part comes every morning.")


def next_get(req):
    return 200, HTML, page("Sending your next part", "One moment. If nothing happens, press the button.",
                           "Send me the next part", req.query.get("t", ""), auto=True)


def next_post(req):
    d = drip()
    sub, outcome = d.send_now(req.data().get("t") or req.query.get("t"))
    title = d.book(sub["book"])["title"]
    if outcome == "sent":
        return 200, HTML, page("On its way", f"Part {sub['sent']} of {title} is heading to your inbox.")
    if outcome == "done":
        return 200, HTML, page("That's the whole book", f"You've had every part of {title}.")
    if outcome == "stopped":
        return 200, HTML, page("You unsubscribed", "We don't send to this address any more.")
    raise Problem(502, "We couldn't send it just now. Try again in a minute.")


def cron(req):
    secret = os.environ.get("CRON_SECRET")
    if not secret or req.headers.get("authorization") != f"Bearer {secret}":
        return 401, JSON, json.dumps({"error": "unauthorized"})
    return 200, JSON, json.dumps(drip().run_daily())


# --- Vercel adapter ---------------------------------------------------------------------
def handler_for(get=None, post=None, kind=HTML, limit=10_000):
    """limit is the largest body taken, in bytes; Vercel stops at 4.5 MB."""
    routes = {"GET": get and guarded(get, kind), "POST": post and guarded(post, kind)}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.run("GET")

        def do_POST(self):
            self.run("POST")

        def run(self, method):
            fn = routes.get(method)
            if not fn:
                status, ctype, body = 405, JSON, json.dumps({"error": "method not allowed"})
            else:
                length = int(self.headers.get("content-length") or 0)
                if length > limit:
                    status, ctype = 413, JSON
                    body = json.dumps({"error": "That's too much to send in one go. Try a shorter PDF."})
                else:
                    body = self.rfile.read(length) if length else b""
                    status, ctype, body = fn(Request(method, self.path, dict(self.headers), body))
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler
