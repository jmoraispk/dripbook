"""Offline tests for drip/app.py and drip/web.py, with fake Redis and Postmark.

Run: python3 -m unittest discover -s drip
"""
import base64
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest import mock

import app
import scan
import web
from app import Drip, Problem

LIBRARY = {"7": {"title": "A Test & Book", "author": "Ann Writer"}, "8": {"title": "Stub", "author": ""}}
PARA = " ".join(["word"] * 100)
BOOK = ("*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\n"
        + "\n\n".join(f"CHAPTER {c}.\n\n" + "\n\n".join([PARA] * 12) for c in range(1, 4))
        + "\n\n*** END OF THE PROJECT GUTENBERG EBOOK X ***\n")   # 3,600 words: 3 parts at 5 min


class FakeRedis:
    def __init__(self):
        self.data, self.ttl, self.calls = {}, {}, 0

    def __call__(self, cmd, *args):
        self.calls += 1
        if cmd == "GET":
            return self.data.get(args[0])
        if cmd == "SET":
            opts = [str(a).upper() for a in args[2:]]
            if "NX" in opts and args[0] in self.data:
                return None
            self.data[args[0]] = args[1]
            self.ttl[args[0]] = int(args[2 + opts.index("EX") + 1]) if "EX" in opts else None
            return "OK"
        if cmd == "EXPIRE":
            self.ttl[args[0]] = int(args[1])
            return 1
        if cmd == "PERSIST":
            self.ttl[args[0]] = None
            return 1
        if cmd == "SADD":
            self.data.setdefault(args[0], set()).add(args[1])
            return 1
        if cmd == "SREM":
            self.data.get(args[0], set()).discard(args[1])
            return 1
        if cmd == "SMEMBERS":
            return list(self.data.get(args[0], set()))
        if cmd == "RPUSH":
            self.data.setdefault(args[0], []).append(args[1])
            return len(self.data[args[0]])
        if cmd == "LLEN":
            return len(self.data.get(args[0], []))
        if cmd == "LRANGE":
            return list(self.data.get(args[0], []))
        if cmd == "HSET":
            self.data.setdefault(args[0], {})[str(args[1])] = args[2]
            return 1
        if cmd == "HGET":
            return self.data.get(args[0], {}).get(str(args[1]))
        if cmd == "DEL":
            return int(self.data.pop(args[0], None) is not None)
        if cmd == "HGETALL":
            return [x for kv in self.data.get(args[0], {}).items() for x in kv]
        if cmd == "ZADD":
            self.data.setdefault(args[0], {})[args[2]] = float(args[1])
            return 1
        if cmd == "ZREMRANGEBYSCORE":
            z = self.data.get(args[0], {})
            for k in [k for k, v in z.items() if v <= float(args[2])]:
                del z[k]
            return 1
        if cmd == "ZCARD":
            return len(self.data.get(args[0], {}))
        if cmd == "SCAN":   # one page; MATCH is a prefix followed by *
            prefix = args[2].rstrip("*")
            return ["0", [k for k in self.data if k.startswith(prefix)]]
        if cmd == "MGET":
            return [self.data.get(k) for k in args]
        raise AssertionError(f"unexpected Redis command {cmd}")


class FakeMail:
    def __init__(self):
        self.sent, self.reply = [], {"ErrorCode": 0}

    def __call__(self, message):
        self.sent.append(message)
        return self.reply


class FakeBlob:
    """A private Vercel Blob store in memory; fail makes every call raise."""
    def __init__(self):
        self.files, self.ttl, self.fail = {}, {}, False

    def put(self, pathname, data, content_type, ttl_days):
        if self.fail:
            raise OSError("blob down")
        url = f"https://store.private.blob.vercel-storage.com/{pathname}"
        self.files[url], self.ttl[url] = data, ttl_days
        return url

    def get(self, url):
        if self.fail:
            raise OSError("blob down")
        return self.files[url]

    def set_ttl(self, urls, days):
        for u in urls:
            self.ttl[u] = days


class Clock:
    def __init__(self):
        self.now, self.date = 1_000_000, "2026-11-16"


def make(env=None, page=None):
    """A Drip on fakes. page is the HTML edition the mirror serves for book 7 (none by default)."""
    clock = Clock()
    fetched = []

    def fetch(book_id):
        fetched.append(book_id)
        return BOOK if book_id in ("7", "77") else "*** START OF THE PROJECT GUTENBERG EBOOK S ***\n\nMusic files.\n"

    def fetch_html(book_id):
        if page is None or book_id != "7":
            raise OSError("404")
        return page
    d = Drip(FakeRedis(), FakeMail(), env=env or {}, fetch=fetch, library=LIBRARY,
             clock=lambda: clock.now, day=lambda: clock.date, fetch_html=fetch_html,
             catalog={"77": {"title": "A Book Outside the Library", "author": "Someone Long Dead"},
                      "78": {"title": "Only Music", "author": ""}})
    return d, clock, fetched


def html_edition(words_per_para=100):
    """Book 7 as Gutenberg's HTML edition, with headings, an example and page numbers."""
    para = " ".join(["word"] * words_per_para)
    return ("<html><body><header id='pg-header'><p>Licence</p></header>"
            + "".join(f"<span class='pagenum'>{{{c}}}</span><h2>CHAPTER {c}.</h2>"
                      + "".join(f"<p>{para}</p>" for _ in range(11))
                      + f"<div class='example'><p>{para}</p></div>" for c in range(1, 4))
            + "</body></html>")


def token_from(message, action):
    body = message["TextBody"]
    start = body.index(f"/api/{action}?t=") + len(f"/api/{action}?t=")
    return body[start:].split()[0]


class SubscribeTest(unittest.TestCase):
    def test_sends_a_confirmation_on_the_transactional_stream(self):
        d, _, _ = make()
        out = d.subscribe(" Me@Example.com ", "7", "5", "https://preview.example")
        self.assertEqual(out["status"], "sent")
        (msg,) = d.mail.sent
        self.assertEqual(msg["MessageStream"], "outbound")
        self.assertEqual(msg["To"], "me@example.com")
        self.assertIn("A Test &amp; Book", msg["HtmlBody"])
        self.assertIn("3 parts", msg["TextBody"])
        token = token_from(msg, "confirm")
        self.assertIn(f"https://preview.example/api/confirm?t={token}", msg["TextBody"])
        sub = d.get_sub(token)
        self.assertEqual((sub["status"], sub["pace"]), ("pending", 5))
        self.assertEqual(d.redis.ttl[f"sub:{token}"], app.PENDING_SECONDS)

    def test_time_zone_is_saved_when_it_looks_valid(self):
        d, clock, _ = make()
        for tz, saved in [("Europe/Lisbon", "Europe/Lisbon"), ("America/Argentina/Buenos_Aires",
                          "America/Argentina/Buenos_Aires"), ("UTC", "UTC"), ("<script>", ""), (None, "")]:
            clock.now += 61
            d.subscribe("a@b.co", "7", 5, "https://x", tz)
            self.assertEqual(d.get_sub(token_from(d.mail.sent[-1], "confirm"))["tz"], saved)

    def test_rejects_bad_input(self):
        d, _, _ = make()
        for email, book, pace in [("nope", "7", 5), ("a@b.co", "999", 5), ("a@b.co", "7", 7), ("a@b.co", "7", None)]:
            with self.assertRaises(app.Problem) as e:
                d.subscribe(email, book, pace, "https://x")
            self.assertEqual(e.exception.status, 400)
        self.assertEqual(d.mail.sent, [])

    def test_stub_books_are_refused(self):
        d, _, _ = make()
        with self.assertRaises(app.Problem) as e:
            d.subscribe("a@b.co", "8", 5, "https://x")
        self.assertEqual(e.exception.status, 422)

    def test_allowlist(self):
        d, _, _ = make({"ALLOWED_EMAILS": "me@x.com, @team.com"})
        d.subscribe("me@x.com", "7", 5, "https://x")
        d.subscribe("anyone@team.com", "7", 5, "https://x")
        with self.assertRaises(app.Problem) as e:
            d.subscribe("stranger@y.com", "7", 5, "https://x")
        self.assertEqual(e.exception.status, 403)

    def test_book_is_fetched_once_and_cached(self):
        d, _, fetched = make()
        d.subscribe("a@b.co", "7", 5, "https://x")
        d2 = Drip(d.redis, d.mail, env={}, fetch=None, library=LIBRARY)
        self.assertEqual(d2.book("7")["blocks"], d.book("7")["blocks"])
        self.assertEqual(fetched, ["7"])

    def test_html_edition_is_preferred_when_it_matches_the_text(self):
        d, _, _ = make(page=html_edition())
        kinds = {k for k, _ in d.book("7")["blocks"]}
        self.assertTrue({"pg", "quote", "h2"} <= kinds, kinds)
        d.subscribe("a@b.co", "7", 5, "https://x")
        d.confirm(token_from(d.mail.sent[0], "confirm"))
        self.assertIn("Pages 1–2 of 3", d.mail.sent[-1]["TextBody"])
        self.assertIn("<blockquote", d.mail.sent[-1]["HtmlBody"])

    def test_plain_text_when_the_html_edition_is_short(self):
        d, _, _ = make(page=html_edition(words_per_para=10))
        self.assertNotIn("pg", {k for k, _ in d.book("7")["blocks"]})

    def test_resend_is_rate_limited_and_keeps_the_token(self):
        d, clock, _ = make()
        d.subscribe("a@b.co", "7", 5, "https://x")
        with self.assertRaises(app.Problem) as e:
            d.subscribe("a@b.co", "7", 5, "https://x")
        self.assertEqual(e.exception.status, 429)
        clock.now += 61
        d.subscribe("a@b.co", "7", 10, "https://x")
        first, second = (token_from(m, "confirm") for m in d.mail.sent)
        self.assertEqual(first, second)
        self.assertEqual(d.get_sub(first)["pace"], 10)

    def test_postmark_failure_is_reported(self):
        d, _, _ = make()
        d.mail.reply = {"ErrorCode": 300, "Message": "Invalid email"}
        with self.assertRaises(app.Problem) as e:
            d.subscribe("a@b.co", "7", 5, "https://x")
        self.assertEqual(e.exception.status, 502)


class RequestTest(unittest.TestCase):
    def saved(self, d):
        return [json.loads(v) for v in d.redis.data.get(app.REQUESTS, [])]

    def test_keeps_every_request(self):
        d, clock, _ = make()
        out = d.request_book("  The   Road ", "Cormac McCarthy", " Me@X.com ", sender="1.2.3.4")
        self.assertEqual(out["status"], "saved")
        self.assertIn("The Road", out["message"])
        clock.now += 10
        d.request_book("The Road", "", "me@x.com", mine=True, sender="1.2.3.4")
        d.request_book("The Road", "Cormac McCarthy", "me@x.com", sender="5.6.7.8")
        self.assertEqual([(r["title"], r["author"], r["mine"]) for r in self.saved(d)],
                         [("The Road", "Cormac McCarthy", False), ("The Road", "", True),
                          ("The Road", "Cormac McCarthy", False)])
        self.assertEqual(self.saved(d)[1]["at"], 1_000_010)
        self.assertEqual(d.mail.sent, [], "a request sends no email")

    def test_an_identical_request_moments_later_is_dropped(self):
        d, _, _ = make()
        d.request_book("Dune", "", "a@b.co", sender="1.2.3.4")
        out = d.request_book("Dune", "", "a@b.co", sender="1.2.3.4")
        self.assertEqual(out["status"], "saved", "the reader still sees thanks")
        self.assertEqual(len(self.saved(d)), 1)
        self.assertEqual(d.redis.ttl[next(k for k in d.redis.data if k.startswith("request-seen:"))], 600)

    def test_rejects_bad_input(self):
        d, _, _ = make()
        for title, email in (("", "a@b.co"), ("x" * 201, "a@b.co"), ("Dune", "nope")):
            with self.assertRaises(app.Problem):
                d.request_book(title, "", email)

    def test_a_gutenberg_book_from_search_keeps_its_number(self):
        d, _, _ = make()
        out = d.request_book("The Philosophy of the Beautiful", "William Knight", "a@b.co", gutenberg="12345")
        self.assertIn("when The Philosophy of the Beautiful is ready", out["message"])
        d.request_book("Emma", "", "a@b.co")
        self.assertEqual([r.get("gutenberg") for r in self.saved(d)], [12345, None])
        for bad in ("12a", "-1", "12345678"):
            with self.assertRaises(app.Problem):
                d.request_book("Dune", "", "a@b.co", gutenberg=bad)

    def test_a_scan_from_search_keeps_its_id(self):
        d, _, _ = make()
        out = d.request_book("Miss Marjoribanks", "Margaret Oliphant", "a@b.co", source="ia:missmarjoribanks00olip")
        self.assertIn("when Miss Marjoribanks is ready", out["message"])
        self.assertEqual([r.get("source") for r in self.saved(d)], ["ia:missmarjoribanks00olip"])
        for bad in ("gutenberg:12", "hathi:uc1.$b123456", "ia:", "ia:a b", "ia:<x>", "ia:" + "x" * 121):
            with self.assertRaises(app.Problem):
                d.request_book("Dune", "", "a@b.co", source=bad)

    def test_stops_taking_requests_when_full(self):
        d, _, _ = make()
        d.request_book("Dune", "", "a@b.co")
        with mock.patch.object(app, "MAX_REQUESTS", 1):
            with self.assertRaises(app.Problem) as e:
                d.request_book("Emma", "", "a@b.co")
        self.assertEqual(e.exception.status, 503)


def lessons(n=3):
    """What the cut editor posts: lessons as [[kind, html], ...]."""
    para = " ".join(["word"] * 300)
    return [[["h2", f"Chapter {i}"], ["pg", str(i * 10)], ["p", f"<em>{para}</em>"], ["quote", "A <b>quote</b>"]]
            for i in range(1, n + 1)]


class OwnTest(unittest.TestCase):
    def start(self, d, **kw):
        args = dict(email="Me@X.com", title="My Notes", lessons=lessons(), base="https://x", rights=True)
        args.update(kw)
        return d.subscribe_own(args.pop("email"), args.pop("title"), args.pop("lessons"), args.pop("base"),
                               rights=args.pop("rights"))

    def test_whole_drip_of_an_own_pdf(self):
        d, clock, fetched = make()
        out = self.start(d)
        self.assertEqual(out["status"], "sent")
        confirm = d.mail.sent[0]
        self.assertIn("from the PDF you cut into 3 parts", confirm["TextBody"])
        self.assertIs(confirm["TrackOpens"], False)
        token = token_from(confirm, "confirm")
        sub = d.get_sub(token)
        book_key = sub["book"]
        self.assertTrue(book_key.startswith("own:"))
        self.assertEqual(d.redis.ttl[book_key], app.PENDING_SECONDS)

        d.confirm(token)
        self.assertIsNone(d.redis.ttl[book_key], "kept while the drip runs")
        first = d.mail.sent[1]
        self.assertEqual(first["Subject"], "My Notes, part 1 of 3")
        self.assertIn("Chapter 1", first["HtmlBody"])
        self.assertIn("Page 10", first["HtmlBody"])
        self.assertIn("<em>word", first["HtmlBody"])
        self.assertIn("A &lt;b&gt;quote&lt;/b&gt;", first["HtmlBody"], "other tags are shown as text")
        self.assertIn("From the PDF you sent us", first["HtmlBody"])
        for _ in range(2):
            clock.date = clock.date[:-2] + str(int(clock.date[-2:]) + 1)
            d.run_daily()
        self.assertEqual(d.get_sub(token)["status"], "done")
        self.assertEqual(d.redis.ttl[book_key], app.OWN_KEEP_SECONDS)
        self.assertEqual(fetched, [], "nothing is fetched from Gutenberg")

    def test_checks(self):
        d, _, _ = make()
        for kw, status in ((dict(rights=False), 400), (dict(email="nope"), 400), (dict(lessons=[]), 400),
                           (dict(lessons=[[["p", 5]]]), 400), (dict(lessons=[[["pg", "3"]]]), 400)):
            with self.assertRaises(Problem, msg=kw) as e:
                self.start(d, **kw)
            self.assertEqual(e.exception.status, status, kw)
        self.start(d)
        with self.assertRaises(app.Problem) as e:
            self.start(d)
        self.assertEqual(e.exception.status, 429)

    def test_allowlist_and_ceiling(self):
        d, _, _ = make(env={"ALLOWED_EMAILS": "@team.com"})
        with self.assertRaises(app.Problem) as e:
            self.start(d)
        self.assertEqual(e.exception.status, 403)
        d, _, _ = make()
        with mock.patch.object(app, "MAX_OWN_BOOKS", 1):
            self.start(d)
            with self.assertRaises(app.Problem) as e:
                self.start(d, email="b@x.com")
        self.assertEqual(e.exception.status, 503)

    def picture_lessons(self):
        out = lessons()
        out[0][2:2] = [["img", "i0 480"], ["note", "Figure 1: a map"], ["img", "i1 9999"]]
        out[2].append(["img", "i2 300"])
        return out

    def test_pictures_go_inline(self):
        d, clock, _ = make()
        d.blob = FakeBlob()
        png = base64.b64encode(b"\x89PNG fake").decode()
        out = self.start(d, lessons=self.picture_lessons())
        self.assertEqual(out["status"], "upload")
        self.assertEqual(d.mail.sent, [], "no email until the pictures are in")
        book, key = out["book"], out["key"]
        for bad, status in ((dict(key="wrong"), 403), (dict(part=2), 400), (dict(part=9), 400),
                            (dict(images={"i9": {"type": "image/png", "data": png}}), 400),
                            (dict(images={"i0": {"type": "image/gif", "data": png}}), 400),
                            (dict(images={"i0": {"type": "image/png", "data": "not base64!"}}), 400)):
            args = dict(key=key, part=1, images={"i0": {"type": "image/png", "data": png}})
            args.update(bad)
            with self.assertRaises(Problem, msg=bad) as e:
                d.add_pictures(book, args["key"], args["part"], args["images"])
            self.assertEqual(e.exception.status, status, bad)
        d.add_pictures(book, key, 1, {"i0": {"type": "image/png", "data": png}})
        d.add_pictures(book, key, 1, {"i1": {"type": "image/jpeg", "data": png}})
        self.assertEqual(sorted(d.blob.ttl.values()), [8, 8], "unconfirmed: gone in 8 days")
        self.assertEqual(len(d.picture_files(book, 1)), 2, "a second request makes a second file")

        out = d.pictures_done(book, key)
        self.assertEqual(out["status"], "sent")
        with self.assertRaises(app.Problem):
            d.pictures_done(book, key)   # the key is used up
        d.confirm(token_from(d.mail.sent[0], "confirm"))
        self.assertEqual(set(d.blob.ttl.values()), {3 + 60}, "kept for the drip, with room to spare")
        first = d.mail.sent[1]
        self.assertEqual([a["ContentID"] for a in first["Attachments"]], ["cid:i0.png", "cid:i1.jpg"])
        self.assertEqual(first["Attachments"][0]["Content"], png)
        html_body = first["HtmlBody"]
        self.assertLess(html_body.index('src="cid:i0.png" width="480"'), html_body.index("Figure 1: a map"))
        self.assertIn('src="cid:i1.jpg" width="560"', html_body, "never wider than the email")
        self.assertNotIn("i0", first["TextBody"])

        clock.date = "2026-11-17"
        d.run_daily()
        self.assertNotIn("Attachments", d.mail.sent[2], "part 2 has no pictures")
        clock.date = "2026-11-18"
        d.blob.fail = True
        d.run_daily()
        last = d.mail.sent[3]
        self.assertNotIn("Attachments", last, "Blob down: the part still goes, without its picture")
        self.assertNotIn("cid:", last["HtmlBody"])
        d.blob.fail = False
        self.assertEqual(set(d.blob.ttl.values()), {31}, "deleted 30 days after the drip ends")
        self.assertEqual(d.redis.ttl[app.OWN_PICS + book[len(app.OWN):]], app.OWN_KEEP_SECONDS)

    def test_pictures_without_blob_keep_captions(self):
        d, _, _ = make()
        out = self.start(d, lessons=self.picture_lessons())
        self.assertEqual(out["status"], "sent")
        parts = d.book(d.get_sub(token_from(d.mail.sent[0], "confirm"))["book"])["parts"]
        self.assertNotIn("img", [k for part in parts for k, _ in part])
        self.assertIn(("note", "Figure 1: a map"), parts[0])

    def test_picture_limits(self):
        d, _, _ = make()
        d.blob = FakeBlob()
        out = self.start(d, lessons=self.picture_lessons())
        big = {"i0": {"type": "image/jpeg", "data": base64.b64encode(b"x" * 4_000_000).decode()}}
        with mock.patch.object(app, "MAX_PART_PICTURE_BYTES", 3_000_000):
            with self.assertRaises(app.Problem) as e:
                d.add_pictures(out["book"], out["key"], 1, big)
        self.assertEqual(e.exception.status, 413)
        d.blob.fail = True
        with self.assertRaises(app.Problem) as e:
            d.add_pictures(out["book"], out["key"], 3, {"i2": {"type": "image/png", "data": "AAAA"}})
        self.assertEqual(e.exception.status, 502)
        self.assertEqual(d.pictures_done(out["book"], out["key"])["status"], "sent", "the drip goes without them")

    def test_blob_only_sends_its_token_to_blob(self):
        with self.assertRaises(ValueError):
            app.Blob("tok").get("https://evil.example/own/x/1-0.json")

    def test_safe_inline(self):
        self.assertEqual(app.safe_inline('<em>a</em> &amp; <script>x</script><strong>b'),
                         "<em>a</em> &amp; &lt;script&gt;x&lt;/script&gt;<strong>b</strong>")


class DripTest(unittest.TestCase):
    def setUp(self):
        self.d, self.clock, _ = make()
        self.d.subscribe("a@b.co", "7", 5, "https://x")
        self.token = token_from(self.d.mail.sent[0], "confirm")

    def test_whole_book(self):
        d, clock = self.d, self.clock
        sub, outcome = d.confirm(self.token)
        self.assertEqual(outcome, "sent")
        part1 = d.mail.sent[-1]
        self.assertEqual(part1["MessageStream"], "broadcast-1")
        self.assertEqual(part1["Subject"], "A Test & Book, part 1 of 3")
        self.assertIn("{{{ pm:unsubscribe }}}", part1["HtmlBody"])
        self.assertIn("{{{ pm:unsubscribe }}}", part1["TextBody"])
        self.assertIn(f"https://x/api/next?t={self.token}", part1["TextBody"])
        self.assertIn("CHAPTER 1.", part1["HtmlBody"])
        self.assertIn("A Test &amp; Book · Part 1 of 3", part1["HtmlBody"])
        self.assertIn("Chapter 1.", part1["TextBody"].splitlines()[1], "the chapter is named at the top")
        self.assertIn("0% through · ", part1["TextBody"])
        self.assertIsNone(d.redis.ttl[f"sub:{self.token}"], "confirmed subscriptions don't expire")

        self.assertEqual(d.confirm(self.token)[1], "already")
        self.assertEqual(d.run_daily(), {"skipped": 1}, "part 1 went out today")

        clock.date = "2026-11-17"
        self.assertEqual(d.run_daily(), {"sent": 1})
        self.assertEqual(d.mail.sent[-1]["Subject"], "A Test & Book, part 2 of 3")
        self.assertEqual(d.run_daily(), {"skipped": 1}, "a cron retry the same day sends nothing")

        _, outcome = d.send_now(self.token)          # "send me the next part now"
        self.assertEqual(outcome, "sent")
        last = d.mail.sent[-1]
        self.assertEqual(last["Subject"], "A Test & Book, part 3 of 3")
        self.assertIn("end of A Test", last["TextBody"])
        self.assertNotIn("/api/next", last["TextBody"])
        self.assertEqual(d.get_sub(self.token)["status"], "done")
        self.assertEqual(d.redis("SMEMBERS", "active"), [])

        clock.date = "2026-11-18"
        count = len(d.mail.sent)
        self.assertEqual(d.run_daily(), {})
        self.assertEqual(d.send_now(self.token)[1], "done")
        self.assertEqual(len(d.mail.sent), count)

    def test_unsubscribed_reader_is_stopped(self):
        d = self.d
        d.confirm(self.token)
        d.mail.reply = {"ErrorCode": 406, "Message": "Inactive recipient"}
        self.clock.date = "2026-11-17"
        self.assertEqual(d.run_daily(), {"stopped": 1})
        self.assertEqual(d.get_sub(self.token)["status"], "stopped")
        self.assertEqual(d.redis("SMEMBERS", "active"), [])

    def test_failed_send_is_retried_next_run(self):
        d = self.d
        d.confirm(self.token)
        self.clock.date = "2026-11-17"
        d.mail.reply = {"ErrorCode": 500, "Message": "oops"}
        self.assertEqual(d.run_daily(), {"error": 1})
        d.mail.reply = {"ErrorCode": 0}
        self.assertEqual(d.run_daily(), {"sent": 1})
        self.assertEqual(d.get_sub(self.token)["sent"], 2)

    def test_resubscribing_while_active_sends_nothing(self):
        d = self.d
        d.confirm(self.token)
        count = len(d.mail.sent)
        self.assertEqual(d.subscribe("a@b.co", "7", 5, "https://x")["status"], "already")
        self.assertEqual(len(d.mail.sent), count)

    def test_unknown_token(self):
        with self.assertRaises(app.Problem) as e:
            self.d.confirm("nope")
        self.assertEqual(e.exception.status, 404)


class WebTest(unittest.TestCase):
    """The real http.server handler classes, on a local port."""
    def setUp(self):
        self.d, self.clock, _ = make()
        quiet = mock.patch("http.server.BaseHTTPRequestHandler.log_message")
        quiet.start()
        self.addCleanup(quiet.stop)
        patcher = mock.patch.object(web, "drip", lambda mail=True: self.d)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.servers = {}
        for name, h in {"subscribe": web.handler_for(post=web.subscribe, kind=web.JSON),
                        "confirm": web.handler_for(get=web.confirm_get, post=web.confirm_post),
                        "next": web.handler_for(get=web.next_get, post=web.next_post),
                        "cron": web.handler_for(get=web.cron, kind=web.JSON),
                        "request": web.handler_for(post=web.request_book, kind=web.JSON),
                        "own": web.handler_for(post=web.subscribe_own, kind=web.JSON, limit=2000),
                        "scan": web.handler_for(get=web.scan_get, kind=web.JSON)}.items():
            srv = HTTPServer(("127.0.0.1", 0), h)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            self.servers[name] = f"http://127.0.0.1:{srv.server_port}/api/{name}"

    def call(self, name, query="", data=None, json_body=None, headers=None):
        body, hdrs = None, dict(headers or {})
        if json_body is not None:
            body, hdrs["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif data is not None:
            body, hdrs["Content-Type"] = data.encode(), "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.servers[name] + query, data=body, headers=hdrs)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    def test_sign_up_confirm_and_read_ahead(self):
        status, body = self.call("subscribe", json_body={"email": "a@b.co", "book": "7", "pace": 5, "tz": "Asia/Tokyo"},
                                 headers={"X-Forwarded-Host": "dripbook-git-x.vercel.app"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "sent")
        token = token_from(self.d.mail.sent[0], "confirm")
        self.assertIn("https://dripbook-git-x.vercel.app/api/confirm", self.d.mail.sent[0]["TextBody"])
        self.assertEqual(self.d.get_sub(token)["tz"], "Asia/Tokyo")

        status, body = self.call("confirm", f"?t={token}")
        self.assertEqual(status, 200)
        self.assertIn('method="post"', body)
        self.assertIn('getElementById("go").submit()', body, "a browser posts the form by itself")
        self.assertEqual(len(self.d.mail.sent), 1, "opening the link alone sends nothing")

        status, body = self.call("confirm", data=f"t={token}")
        self.assertEqual(status, 200)
        self.assertIn("Part 1 of A Test &amp; Book is on its way", body)

        status, body = self.call("next", data=f"t={token}")
        self.assertIn("Part 2", body)
        self.assertEqual(len(self.d.mail.sent), 3)

    def test_scan_pieces_are_cached_but_answers_are_not(self):
        self.d.ia = FakeArchive()
        with urllib.request.urlopen(self.servers["scan"] + "?id=ia:philosophy00knig&step=pdf&at=0") as r:
            self.assertEqual(r.headers["Content-Type"], "application/pdf")
            self.assertIn("s-maxage", r.headers["Cache-Control"])
            self.assertEqual(r.read(), self.d.ia.pdf[:app.PDF_PIECE])
        with urllib.request.urlopen(self.servers["scan"] + "?id=gutenberg:77&step=find") as r:
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            self.assertEqual(json.loads(r.read())["id"], "77")
        status, body = self.call("scan", "?id=gutenberg:99999&step=find")
        self.assertEqual(status, 400)

    def test_errors(self):
        status, body = self.call("subscribe", json_body={"email": "bad", "book": "7", "pace": 5})
        self.assertEqual((status, json.loads(body)["error"]), (400, "That email address doesn't look right."))
        status, body = self.call("confirm", data="t=nope")
        self.assertEqual(status, 404)
        self.assertIn("expired", body)
        status, _ = self.call("subscribe")   # GET
        self.assertEqual(status, 405)

    def test_book_request(self):
        status, body = self.call("request", json_body={"title": "Dune", "email": "a@b.co", "mine": True})
        self.assertEqual(status, 200, body)
        self.assertIn("set up Dune with you", json.loads(body)["message"])
        self.assertTrue(json.loads(self.d.redis.data[app.REQUESTS][0])["mine"])
        status, body = self.call("request", json_body={"title": "", "email": "a@b.co"})
        self.assertEqual((status, json.loads(body)["error"]), (400, "Tell us the book's title."))
        status, _ = self.call("request", json_body={"title": "Spam", "email": "a@b.co", "website": "x.biz"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.d.redis.data[app.REQUESTS]), 1, "the hidden field catches bots")

    def test_own_pdf(self):
        status, body = self.call("own", json_body={"email": "a@b.co", "title": "Mine", "rights": True,
                                                   "lessons": [[["p", "Hello there."]]]})
        self.assertEqual(status, 200, body)
        self.assertIn("lesson 1", json.loads(body)["message"])
        status, body = self.call("own", json_body={"email": "a@b.co", "lessons": [[["p", "x" * 3000]]]})
        self.assertEqual(status, 413)

    def test_cron_needs_the_secret(self):
        with mock.patch.dict(os.environ, {"CRON_SECRET": "s3cret"}):
            self.assertEqual(self.call("cron")[0], 401)
            self.assertEqual(self.call("cron", headers={"Authorization": "Bearer wrong"})[0], 401)
            status, body = self.call("cron", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual((status, json.loads(body)), (200, {}))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.call("cron", headers={"Authorization": "Bearer "})[0], 401)


class EntryPointsTest(unittest.TestCase):
    def test_api_files_expose_a_handler(self):
        import importlib.util
        from http.server import BaseHTTPRequestHandler
        api = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api")
        for name in ("subscribe", "confirm", "next", "cron", "request", "own", "pictures"):
            spec = importlib.util.spec_from_file_location(f"api_{name}", os.path.join(api, f"{name}.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertTrue(issubclass(mod.handler, BaseHTTPRequestHandler), name)
            with open(os.path.join(api, f"{name}.py")) as f:   # Vercel's build skips files without it
                self.assertRegex(f.read(), r"(?m)^class handler\(", name)


class LibraryTest(unittest.TestCase):
    def test_real_library_loads(self):
        books = app.load_library()
        self.assertIn("1342", books)
        self.assertEqual(books["1342"]["title"], "Pride and Prejudice")


class ReadBookTest(unittest.TestCase):
    def test_a_plain_text_that_only_points_to_the_html_uses_the_html(self):
        cover = "*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\nThis text file is only a cover for the html file.\n\n*** END"
        page = "<h2>CHAPTER I.</h2>" + "<p>" + "word " * 800 + "end.</p>"
        found = app.read_book(cover, page)
        self.assertEqual(found[0], ("h2", "CHAPTER I."))
        self.assertGreater(sum(len(t.split()) for _, t in found), 800)


SENTENCE = ("The beautiful is that which pleases the mind without a concept, and the history of the theory "
            "of beauty shows how slowly the thinkers of each age came to see it plainly").split()


def djvu(pages=24, words=None):
    """A scan's OCR as the Archive serves it: pages of paragraphs of lines of words."""
    words = words or SENTENCE
    out, k = ["<DjVuXML><BODY>"], 0
    for page in range(pages):
        out.append("<OBJECT>")
        if page % 8 == 0:
            out.append(f'<PARAGRAPH><LINE><WORD coords="0,60,10,0">CHAPTER</WORD>'
                       f'<WORD coords="0,60,10,0">{"I" * (page // 8 + 1)}.</WORD></LINE></PARAGRAPH>')
        for _ in range(3):
            out.append("<PARAGRAPH>")
            for _ in range(6):
                line = [words[(k + i) % len(words)] for i in range(10)]
                k += 10
                out.append("<LINE>" + "".join(f'<WORD coords="0,40,10,0">{w}</WORD>' for w in line) + "</LINE>")
            out.append("</PARAGRAPH>")
        out.append("</OBJECT>")
    out.append("</BODY></DjVuXML>")
    return "".join(out).encode()


class FakeArchive:
    def __init__(self, ocr=None, **info):
        self.xml = ocr or djvu()
        self.info = {"title": "The philosophy of the beautiful : being outlines / by William Knight.",
                     "author": "Knight, William Angus, 1836-1916", "year": "1891", "rights": "",
                     "xml": "x_djvu.xml", "xml_size": len(self.xml), "hosts": [],
                     "pdf": "x.pdf", "pdf_size": len(self.pdf), **info}
        self.asked, self.downloads = 0, 0

    def ia_files(self, identifier):
        self.asked += 1
        return dict(self.info)

    def download_xml(self, identifier, info, timeout=60):
        self.downloads += 1
        return self.xml

    hathi = ""                               # HathiTrust's word: 'pd', 'ic', '' (doesn't hold it) or an error to raise
    hathi_asked = 0

    pdf = bytes(range(256)) * 40000          # about 10 MB

    def download_range(self, identifier, info, start, end, timeout=30):
        return self.pdf[start:end + 1]

    def hathi_rights(self, oclcs, year=""):
        self.hathi_asked += 1
        if isinstance(self.hathi, Exception):
            raise self.hathi
        return self.hathi


def make_scan(archive=None, redis=None):
    d, clock, _ = make()
    d.ia = archive or FakeArchive()
    if redis:
        d.redis = redis
    return d, clock


class GutenbergLiveTest(unittest.TestCase):
    def test_open_a_catalog_book_then_start_it(self):
        d, clock, fetched = make()
        found = d.scan_find("gutenberg:77")
        self.assertEqual((found["ready"], found["id"], found["title"]), (False, "77", "A Book Outside the Library"))
        seen = d.scan_preview("77")
        self.assertEqual(seen["title"], "A Book Outside the Library")
        self.assertGreaterEqual(seen["parts"]["5"], seen["parts"]["20"])
        self.assertTrue(seen["first"]["html"])
        self.assertTrue(d.scan_find("77")["ready"])
        out = d.subscribe("me@example.com", "77", 10, "https://x")
        self.assertEqual(out["status"], "sent")
        text = d.scan_text("77")
        self.assertTrue(text["blocks"] and text["starts"])
        self.assertEqual(fetched.count("77"), 1)

    def test_only_catalog_and_library_books(self):
        d, _, _ = make()
        for bad in ("gutenberg:99999", "99999", "gutenberg:", "gutenberg:7a"):
            with self.assertRaises(app.Problem):
                d.scan_find(bad)
        with self.assertRaises(app.Problem):
            d.subscribe("me@example.com", "99999", 10, "https://x")

    def test_a_catalog_book_with_no_text_is_refused(self):
        d, _, _ = make()
        d.scan_find("78")
        with self.assertRaises(app.Problem) as e:
            d.scan_preview("78")
        self.assertEqual(e.exception.status, 422)


class ScanTest(unittest.TestCase):
    def test_text_for_the_editor_matches_the_site_cut(self):
        d, _ = make_scan()
        out = d.scan_text("ia:philosophy00knig")
        self.assertTrue(out["starts"] and out["starts"] == sorted(set(out["starts"])))
        book = d.scan("ia:philosophy00knig")
        parts = app.cut.cut_blocks(book["blocks"], app.EDITOR_PACE)
        self.assertEqual(len(out["starts"]), len(parts))
        self.assertEqual(out["blocks"][out["starts"][1]], list(parts[1][0]))
        self.assertTrue(all(k not in ("pg", "img") for k, _ in out["blocks"]))
        kind, body = d.scan_api({"id": "ia:philosophy00knig", "step": "text"})
        self.assertEqual(json.loads(body)["blocks"], out["blocks"])

    def test_pdf_comes_in_pieces(self):
        d, _ = make_scan()
        head = json.loads(d.scan_api({"id": "ia:philosophy00knig", "step": "pdf"})[1])
        self.assertEqual(head["size"], len(d.ia.pdf))
        got = b""
        while len(got) < head["size"]:
            kind, piece = d.scan_api({"id": "ia:philosophy00knig", "step": "pdf", "at": str(len(got))})
            self.assertEqual(kind, "application/pdf")
            self.assertLessEqual(len(piece), app.PDF_PIECE)
            got += piece
        self.assertEqual(got, d.ia.pdf)
        for bad in ({"at": "-1"}, {"at": str(head["size"])}, {"at": "x"}, {"step": "nope"}):
            with self.assertRaises(app.Problem):
                d.scan_api({"id": "ia:philosophy00knig", "step": "pdf", **bad})
        late = FakeArchive(year="1950")                  # rights still checked
        d2, _ = make_scan(late)
        with self.assertRaises(app.Problem):
            d2.scan_api({"id": "ia:philosophy00knig", "step": "pdf", "at": "0"})

    def test_hathitrust_vouches_for_a_later_printing(self):
        late = FakeArchive(year="1941")                     # the Archive alone wouldn't send a 1941 book
        with self.assertRaises(app.Problem):
            make_scan(late)[0].scan_find("ia:later00x")
        late = FakeArchive(year="1941")
        late.hathi = "pd"
        self.assertEqual(make_scan(late)[0].scan_find("ia:later00x")["id"], "ia:later00x")

    def test_hathitrust_in_copyright_is_refused_whatever_the_archive_says(self):
        arch = FakeArchive(rights="NOT_IN_COPYRIGHT")
        arch.hathi = "ic"
        d, _ = make_scan(arch)
        with self.assertRaises(app.Problem) as e:
            d.scan_find("ia:philosophy00knig")
        self.assertIn("HathiTrust", e.exception.message)
        self.assertEqual(d.ia.downloads, 0)

    def test_hathitrust_word_is_kept_but_silence_is_not(self):
        arch = FakeArchive()
        arch.hathi = OSError("down")                       # no answer: the Archive's rules, and ask again next time
        d, _ = make_scan(arch)
        d.scan_find("ia:philosophy00knig")
        arch.hathi = ""
        again, _ = make_scan(arch, redis=d.redis)
        again.scan_find("ia:philosophy00knig")
        third, _ = make_scan(arch, redis=d.redis)
        third.scan_find("ia:philosophy00knig")
        self.assertEqual(arch.hathi_asked, 2)

    def test_hathi_rights_reads_the_catalog(self):
        def catalog(records, items):
            return lambda url, *a, **k: {"oclc:1;oclc:2": {"records": records, "items": items}}
        two = {"r1": {"publishDates": ["1891"]}, "r2": {"publishDates": ["1950"]}}
        items = [{"fromRecord": "r1", "rightsCode": "pd"}, {"fromRecord": "r2", "rightsCode": "ic"}]
        real = scan.get
        try:
            for year, want in (("1891", "pd"), ("1950", "ic"), ("1700", "pd")):   # no record that year: any copy counts
                scan.get = catalog(two, items)
                self.assertEqual(scan.hathi_rights(["1", "2"], year), want, year)
            scan.get = catalog([], [])                        # HathiTrust answers [] when it holds none
            self.assertEqual(scan.hathi_rights(["1", "2"], "1891"), "")
            self.assertEqual(scan.hathi_rights([], "1891"), "")
        finally:
            scan.get = real

    def test_find_then_read_then_cached(self):
        d, _ = make_scan()
        found = d.scan_find("ia:philosophy00knig")
        self.assertEqual((found["ready"], found["title"], found["author"]),
                         (False, "The philosophy of the beautiful", "William Angus Knight"))
        self.assertGreater(found["size"], 0)
        seen = d.scan_preview("ia:philosophy00knig")
        self.assertGreater(seen["words"], 3000)
        self.assertGreater(seen["parts"]["5"], seen["parts"]["20"])
        self.assertIn("beautiful", seen["first"]["html"])
        self.assertEqual(d.ia.downloads, 1)

        again, _ = make_scan(redis=d.redis)          # another visitor, another function instance
        found = again.scan_find("ia:philosophy00knig")
        self.assertTrue(found["ready"])
        self.assertEqual(found["parts"], seen["parts"])
        self.assertEqual((again.ia.asked, again.ia.downloads), (0, 0))

    def test_start_a_scan_and_read_it(self):
        d, clock = make_scan()
        out = d.subscribe("me@example.com", "ia:philosophy00knig", 10, "https://x")
        self.assertEqual(out["status"], "sent")
        token = token_from(d.mail.sent[-1], "confirm")
        _, outcome = d.confirm(token)
        self.assertEqual(outcome, "sent")
        self.assertIn("The philosophy of the beautiful, part 1 of", d.mail.sent[-1]["Subject"])

    def test_only_scans_out_of_us_copyright(self):
        archive = FakeArchive(year="1950")
        d, _ = make_scan(archive)
        with self.assertRaises(app.Problem) as e:
            d.scan_find("ia:later00")
        self.assertEqual(e.exception.status, 422)
        self.assertIn("copyright", e.exception.message)
        with self.assertRaises(app.Problem):
            d.subscribe("me@example.com", "ia:later00", 10, "https://x")
        self.assertEqual((archive.asked, archive.downloads), (1, 0), "the refusal is remembered")
        ok, _ = make_scan(FakeArchive(year="1950", rights="NOT_IN_COPYRIGHT"))
        self.assertFalse(ok.scan_find("ia:later00")["ready"])

    def test_garbled_scans_are_refused(self):
        # 3 words in 10 misread: each line and page survives the cleanup, but the book reads badly
        mixed = [w if i % 10 < 7 else f"w{i}th0{i}t" for i, w in enumerate(SENTENCE * 10)]
        d, _ = make_scan(FakeArchive(ocr=djvu(words=mixed)))
        with self.assertRaises(app.Problem) as e:
            d.scan_preview("ia:junk00")
        self.assertIn("garbled", e.exception.message)

    def test_too_big_or_no_text(self):
        for info, word in (({"xml_size": app.MAX_SCAN_XML + 1}, "too long"), ({"xml": None}, "no text")):
            d, _ = make_scan(FakeArchive(**info))
            with self.assertRaises(app.Problem) as e:
                d.scan_find("ia:big00")
            self.assertIn(word, e.exception.message)

    def test_short_names(self):
        for long, short in (("The Philosophy of the Beautiful ...: Being Outlines of the History", "The Philosophy of the Beautiful"),
                            ("The philosophy of the beautiful : being outlines / by William Knight.", "The philosophy of the beautiful"),
                            ("Miss Marjoribanks", "Miss Marjoribanks")):
            self.assertEqual(scan.short_title(long), short)
        self.assertEqual(scan.short_author("Oliphant, Mrs. (Margaret), 1828-1897"), "Mrs. Oliphant")

    def test_only_scan_ids(self):
        d, _ = make_scan()
        for bad in ("hathi:mdp.39015", "hathi:..\\x", "gutenberg:999", "ia:../etc", "999", ""):
            with self.assertRaises(app.Problem) as e:
                d.scan_find(bad)
            self.assertEqual(e.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
