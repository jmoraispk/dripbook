"""Offline tests for export_signups.py: python3 -m unittest discover -s scripts -p 'test_export_signups.py'"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "drip"))
import export_signups as ex  # noqa: E402
from test_app import FakeRedis  # noqa: E402

LIBRARY = {"7": {"title": "Emma"}}


class Export(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.redis("SET", "sub:t1", json.dumps({"email": "b@x.com", "book": "7", "status": "active", "pace": 10,
                                                "sent": 3, "created": 2_000_000, "confirmed": 2_000_100}))
        self.redis("SET", "sub:t0", json.dumps({"email": "a@x.com", "book": "7", "status": "pending", "pace": 5,
                                                "sent": 0, "created": 1_000_000}))
        self.redis("SET", "who:a@x.com:7", "t0")
        self.redis("SET", "blocks:7", "zipped")
        self.redis("HSET", ex.REQUESTS, "c@x.com|dune", json.dumps(
            {"title": "Dune", "author": "", "email": "c@x.com", "mine": False, "at": 3_000_000}))

    def test_subscribers_oldest_first_with_titles(self):
        rows = ex.subscribers(self.redis, LIBRARY)
        self.assertEqual([r["email"] for r in rows], ["a@x.com", "b@x.com"])
        self.assertEqual((rows[1]["title"], rows[1]["sent"], rows[1]["confirmed"]), ("Emma", 3, "1970-01-24 03:35"))
        self.assertEqual(rows[0]["confirmed"], "")

    def test_writes_both_files(self):
        with tempfile.TemporaryDirectory() as out, mock.patch.object(ex, "Redis", lambda url, token: self.redis), \
                mock.patch.object(ex, "load_library", lambda: LIBRARY), \
                mock.patch.dict("os.environ", {"KV_REST_API_URL": "u", "KV_REST_API_TOKEN": "t"}), \
                mock.patch("builtins.print"):
            ex.main(["--out", out])
            with open(Path(out) / "requests.csv") as f:
                self.assertEqual(list(csv.DictReader(f)),
                                 [{"at": "1970-02-04 17:20", "title": "Dune", "author": "", "email": "c@x.com",
                                   "mine": ""}])
            with open(Path(out) / "subscribers.csv") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 2)


if __name__ == "__main__":
    unittest.main()
