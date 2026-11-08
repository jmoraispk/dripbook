"""Offline tests for send.py: python3 -m unittest discover -s email-spike

Postmark is never called: the HTTP request is faked.
"""
import argparse
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import send  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TextVersionTest(unittest.TestCase):
    def test_strips_tags_and_keeps_paragraphs(self):
        text = send.text_version("<style>p{}</style><h1>Day 1</h1><p>One<br>two</p><p>Three</p>")
        self.assertEqual(text, "Day 1\nOne\ntwo\nThree")

    def test_sample_lesson_reads_as_text(self):
        text = send.text_version(send.SAMPLE)
        self.assertIn("Sun Tzu said", text)
        self.assertNotIn("<", text)


class MessageTest(unittest.TestCase):
    def test_build_message_uses_the_broadcast_stream(self):
        args = argparse.Namespace(sender="Dripbook <lessons@mail.dripbook.ai>", to="a@dripbook.ai",
                                  subject="Day 1", stream="broadcast")
        msg = send.build_message(args, "<p>Hi</p>")
        self.assertEqual(msg["MessageStream"], "broadcast")
        self.assertEqual(msg["TextBody"], "Hi")


class MainTest(unittest.TestCase):
    def run_main(self, argv, reply):
        sent = {}

        def fake_urlopen(req, timeout):
            sent["token"] = req.headers["X-postmark-server-token"]
            sent["body"] = json.loads(req.data)
            return FakeResponse(json.dumps(reply).encode())

        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", fake_urlopen), mock.patch("sys.stdout", out):
            send.main(argv)
        return sent, out.getvalue()

    def test_dry_run_uses_the_test_token(self):
        sent, out = self.run_main(["--to", "a@dripbook.ai", "--dry-run"],
                                  {"ErrorCode": 0, "MessageID": "m1", "To": "a@dripbook.ai"})
        self.assertEqual(sent["token"], "POSTMARK_API_TEST")
        self.assertIn("nothing was delivered", out)

    def test_postmark_error_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(["--to", "a@gmail.com", "--dry-run"], {"ErrorCode": 412, "Message": "Pending approval"})
        self.assertIn("error 412", str(ctx.exception))

    def test_real_send_needs_a_token(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(SystemExit):
            send.main(["--to", "a@dripbook.ai"])


if __name__ == "__main__":
    unittest.main()
