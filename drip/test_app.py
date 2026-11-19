"""Offline tests for drip/app.py and drip/web.py, with fake Redis and Postmark.

Run: python3 -m unittest discover -s drip
"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest import mock

import app
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
            self.data[args[0]] = args[1]
            self.ttl[args[0]] = int(args[3]) if len(args) > 3 else None
            return "OK"
        if cmd == "SADD":
            self.data.setdefault(args[0], set()).add(args[1])
            return 1
        if cmd == "SREM":
            self.data.get(args[0], set()).discard(args[1])
            return 1
        if cmd == "SMEMBERS":
            return list(self.data.get(args[0], set()))
        raise AssertionError(f"unexpected Redis command {cmd}")


class FakeMail:
    def __init__(self):
        self.sent, self.reply = [], {"ErrorCode": 0}

    def __call__(self, message):
        self.sent.append(message)
        return self.reply


class Clock:
    def __init__(self):
        self.now, self.date = 1_000_000, "2026-11-16"


def make(env=None):
    clock = Clock()
    fetched = []

    def fetch(book_id):
        fetched.append(book_id)
        return BOOK if book_id == "7" else "*** START OF THE PROJECT GUTENBERG EBOOK S ***\n\nMusic files.\n"
    d = Drip(FakeRedis(), FakeMail(), env=env or {}, fetch=fetch, library=LIBRARY,
             clock=lambda: clock.now, day=lambda: clock.date)
    return d, clock, fetched


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
            with self.assertRaises(Problem) as e:
                d.subscribe(email, book, pace, "https://x")
            self.assertEqual(e.exception.status, 400)
        self.assertEqual(d.mail.sent, [])

    def test_stub_books_are_refused(self):
        d, _, _ = make()
        with self.assertRaises(Problem) as e:
            d.subscribe("a@b.co", "8", 5, "https://x")
        self.assertEqual(e.exception.status, 422)

    def test_allowlist(self):
        d, _, _ = make({"ALLOWED_EMAILS": "me@x.com, @team.com"})
        d.subscribe("me@x.com", "7", 5, "https://x")
        d.subscribe("anyone@team.com", "7", 5, "https://x")
        with self.assertRaises(Problem) as e:
            d.subscribe("stranger@y.com", "7", 5, "https://x")
        self.assertEqual(e.exception.status, 403)

    def test_book_is_fetched_once_and_cached(self):
        d, _, fetched = make()
        d.subscribe("a@b.co", "7", 5, "https://x")
        d2 = Drip(d.redis, d.mail, env={}, fetch=None, library=LIBRARY)
        self.assertEqual(d2.book("7")["text"], d.book("7")["text"])
        self.assertEqual(fetched, ["7"])

    def test_resend_is_rate_limited_and_keeps_the_token(self):
        d, clock, _ = make()
        d.subscribe("a@b.co", "7", 5, "https://x")
        with self.assertRaises(Problem) as e:
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
        with self.assertRaises(Problem) as e:
            d.subscribe("a@b.co", "7", 5, "https://x")
        self.assertEqual(e.exception.status, 502)


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
        with self.assertRaises(Problem) as e:
            self.d.confirm("nope")
        self.assertEqual(e.exception.status, 404)


class WebTest(unittest.TestCase):
    """The real http.server handler classes, on a local port."""
    def setUp(self):
        self.d, self.clock, _ = make()
        quiet = mock.patch("http.server.BaseHTTPRequestHandler.log_message")
        quiet.start()
        self.addCleanup(quiet.stop)
        patcher = mock.patch.object(web, "drip", lambda: self.d)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.servers = {}
        for name, h in {"subscribe": web.handler_for(post=web.subscribe, kind=web.JSON),
                        "confirm": web.handler_for(get=web.confirm_get, post=web.confirm_post),
                        "next": web.handler_for(get=web.next_get, post=web.next_post),
                        "cron": web.handler_for(get=web.cron, kind=web.JSON)}.items():
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
        self.assertEqual(len(self.d.mail.sent), 1, "opening the link alone sends nothing")

        status, body = self.call("confirm", data=f"t={token}")
        self.assertEqual(status, 200)
        self.assertIn("Part 1 of A Test &amp; Book is on its way", body)

        status, body = self.call("next", data=f"t={token}")
        self.assertIn("Part 2", body)
        self.assertEqual(len(self.d.mail.sent), 3)

    def test_errors(self):
        status, body = self.call("subscribe", json_body={"email": "bad", "book": "7", "pace": 5})
        self.assertEqual((status, json.loads(body)["error"]), (400, "That email address doesn't look right."))
        status, body = self.call("confirm", data="t=nope")
        self.assertEqual(status, 404)
        self.assertIn("expired", body)
        status, _ = self.call("subscribe")   # GET
        self.assertEqual(status, 405)

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
        for name in ("subscribe", "confirm", "next", "cron"):
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


if __name__ == "__main__":
    unittest.main()
