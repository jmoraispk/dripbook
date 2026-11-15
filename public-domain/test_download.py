"""Offline tests for download.py. The network is faked; nothing is fetched."""
import io
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import download


def make_zip(files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in (files or {"book.html": b"<html></html>"}).items():
            z.writestr(name, data)
    return buf.getvalue()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fake_server(routes):
    """A urlopen stand-in. routes maps URL -> bytes, or an exception, or a list of those (one per call)."""
    calls = []

    def urlopen(req, timeout=None):
        url = req.full_url
        calls.append((url, req.get_header("User-agent")))
        item = routes.get(url)
        if isinstance(item, list):
            item = item.pop(0)
        if item is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    urlopen.calls = calls
    return urlopen


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patch = mock.patch("download.time.sleep")
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_fetch(self, routes, name):
        server = fake_server(routes)
        with mock.patch("download.urllib.request.urlopen", server):
            reason = download.fetch("http://x/" + name, self.dir / name)
        return reason, server

    def test_saves_text_and_sends_user_agent(self):
        reason, server = self.run_fetch({"http://x/a.txt": b"hello"}, "a.txt")
        self.assertEqual(reason, "")
        self.assertEqual((self.dir / "a.txt").read_bytes(), b"hello")
        self.assertEqual(server.calls[0][1], "dripbook-catalog/0.1")
        self.assertFalse((self.dir / "a.txt.part").exists())

    def test_saves_valid_zip(self):
        reason, _ = self.run_fetch({"http://x/a.zip": make_zip()}, "a.zip")
        self.assertEqual(reason, "")
        self.assertTrue((self.dir / "a.zip").exists())

    def test_404_fails_at_once_without_retry(self):
        reason, server = self.run_fetch({}, "a.txt")
        self.assertEqual(reason, "HTTP 404")
        self.assertEqual(len(server.calls), 1)
        self.assertFalse((self.dir / "a.txt").exists())

    def test_retries_transient_errors_then_succeeds(self):
        routes = {"http://x/a.txt": [http_error(503), urllib.error.URLError("reset"), b"ok"]}
        reason, server = self.run_fetch(routes, "a.txt")
        self.assertEqual(reason, "")
        self.assertEqual(len(server.calls), 3)
        self.assertEqual((self.dir / "a.txt").read_bytes(), b"ok")

    def test_gives_up_after_all_tries(self):
        reason, server = self.run_fetch({"http://x/a.txt": [http_error(503)] * 4}, "a.txt")
        self.assertEqual(reason, "HTTP 503")
        self.assertEqual(len(server.calls), download.TRIES)

    def test_bad_zip_is_rejected_and_not_kept(self):
        reason, _ = self.run_fetch({"http://x/a.zip": [b"<html>oops</html>"] * 4}, "a.zip")
        self.assertIn("not a valid zip", reason)
        self.assertFalse((self.dir / "a.zip").exists())
        self.assertFalse((self.dir / "a.zip.part").exists())

    def test_empty_text_is_rejected(self):
        reason, _ = self.run_fetch({"http://x/a.txt": [b""] * 4}, "a.txt")
        self.assertEqual(reason, "empty file")


class DownloadAllTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "books"
        patch = mock.patch("download.time.sleep")
        self.sleep = patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def url(self, book_id, fmt):
        return f"{download.MIRROR}/{book_id}/{download.FILENAMES[fmt].format(id=book_id)}"

    def run_all(self, routes, ids, formats=("html", "txt")):
        server = fake_server(routes)
        with mock.patch("download.urllib.request.urlopen", server):
            result = download.download_all(ids, formats, out=self.out, pause=0.5, log=lambda m: None)
        return result, server

    def test_downloads_both_formats_into_book_folders(self):
        routes = {self.url("7", "html"): make_zip(), self.url("7", "txt"): b"text"}
        (downloaded, skipped, failed), _ = self.run_all(routes, ["7"])
        self.assertEqual((downloaded, skipped, failed), (2, 0, 0))
        self.assertTrue((self.out / "7" / "pg7-h.zip").exists())
        self.assertEqual((self.out / "7" / "pg7.txt").read_bytes(), b"text")

    def test_skips_files_already_downloaded(self):
        (self.out / "7").mkdir(parents=True)
        (self.out / "7" / "pg7.txt").write_bytes(b"old")
        routes = {self.url("7", "txt"): b"new"}
        (downloaded, skipped, _), server = self.run_all(routes, ["7"], ["txt"])
        self.assertEqual((downloaded, skipped), (0, 1))
        self.assertEqual(server.calls, [])
        self.assertEqual((self.out / "7" / "pg7.txt").read_bytes(), b"old")

    def test_only_requested_formats(self):
        routes = {self.url("7", "txt"): b"text"}
        _, server = self.run_all(routes, ["7"], ["txt"])
        self.assertEqual([c[0] for c in server.calls], [self.url("7", "txt")])

    def test_failures_are_listed_in_failed_csv(self):
        routes = {self.url("7", "txt"): b"text"}  # no zip on the server -> 404
        (downloaded, _, failed), _ = self.run_all(routes, ["7"])
        self.assertEqual((downloaded, failed), (1, 1))
        self.assertEqual(download.load_failed(self.out / "failed.csv"), {("7", "html"): "HTTP 404"})

    def test_book_with_no_files_leaves_no_empty_folder(self):
        self.run_all({}, ["7"])
        self.assertFalse((self.out / "7").exists())

    def test_rerun_retries_and_clears_earlier_failures(self):
        self.run_all({self.url("7", "txt"): b"text"}, ["7"])
        routes = {self.url("7", "html"): make_zip()}
        (downloaded, skipped, failed), _ = self.run_all(routes, ["7"])
        self.assertEqual((downloaded, skipped, failed), (1, 1, 0))
        self.assertEqual(download.load_failed(self.out / "failed.csv"), {})

    def test_pauses_between_requests_but_not_before_the_first(self):
        routes = {self.url("7", "html"): make_zip(), self.url("7", "txt"): b"text"}
        self.run_all(routes, ["7"])
        self.assertEqual(self.sleep.call_args_list, [mock.call(0.5)])


class ReadIdsTests(unittest.TestCase):
    def test_reads_gutenberg_id_column(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "books.csv"
            path.write_text("rank,gutenberg_id,title\n1,1342,Pride\n2,84,\"Franken, stein\"\n", encoding="utf-8")
            self.assertEqual(download.read_ids(path), ["1342", "84"])

    def test_real_list_has_1000_numeric_ids(self):
        ids = download.read_ids()
        self.assertEqual(len(ids), 1000)
        self.assertTrue(all(i.isdigit() for i in ids))


if __name__ == "__main__":
    unittest.main()
