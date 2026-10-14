"""Offline tests for discover.py: python3 -m unittest discover -s discovery"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover  # noqa: E402

ROW = {
    "creator_name": "Ada Example", "book_title": "Practical Rust", "book_url": "https://leanpub.com/practical-rust",
    "platform": "Leanpub", "topic": "Rust for Python developers", "price_usd": "29",
    "sample_url": "https://leanpub.com/practical-rust/read_sample", "traction": "1,200 readers (Leanpub page)",
    "has_course": "no", "last_active": "2026-08", "contact_channel": "https://example.dev/contact",
    "fit_score": 3, "traction_strength": 4, "notes": "Also wrote Async Rust.",
    "evidence_urls": ["https://leanpub.com/practical-rust"],
}


def block(type_, **kw):
    return NS(type=type_, **kw)


def message(stop_reason, *content):
    usage = NS(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=50,
               server_tool_use=NS(web_search_requests=1, web_fetch_requests=0))
    return NS(stop_reason=stop_reason, content=list(content), usage=usage)


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def stream(self, **params):
        self.calls.append({**params, "messages": list(params["messages"])})
        return contextlib.nullcontext(NS(get_final_message=lambda r=self.responses.pop(0): r))


class CleanRowTest(unittest.TestCase):
    def test_valid_row(self):
        row, problems = discover.clean_row(ROW, [])
        self.assertEqual(problems, [])

    def test_rejects_phone_and_bad_urls(self):
        bad = {**ROW, "contact_channel": "+1 (555) 123-4567", "book_url": "leanpub.com/x", "last_active": "March"}
        _, problems = discover.clean_row(bad, [])
        self.assertEqual(len(problems), 3, problems)

    def test_rejects_duplicate_by_name_or_url(self):
        _, by_name = discover.clean_row({**ROW, "book_url": "https://gumroad.com/l/other"}, [ROW])
        _, by_url = discover.clean_row({**ROW, "creator_name": "Someone Else",
                                        "book_url": "https://www.leanpub.com/practical-rust/"}, [ROW])
        self.assertTrue(by_name and by_url)


class RunPassTest(unittest.TestCase):
    def test_pass_records_resumes_and_notes(self):
        fake = FakeMessages([
            message("pause_turn", block("server_tool_use", name="web_search", input={"query": "leanpub rust"})),
            message("tool_use",
                    block("tool_use", id="t1", name="record_creator", input=ROW),
                    block("tool_use", id="t2", name="record_creator", input=ROW),
                    block("tool_use", id="t3", name="report_inaccessible_source",
                          input={"source": "Gumroad Discover", "url": "https://gumroad.com/discover", "reason": "HTTP 403"})),
            message("end_turn", block("text", text="Leanpub was productive.")),
        ])
        client = NS(beta=NS(messages=fake))
        args = NS(model="claude-opus-5-5", effort="high", target=50, max_turns=10)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            out = Path(tmp)
            state = discover.load_state(out)
            discover.run_pass(client, args, state, "leanpub", "system", out)

            self.assertEqual(len(state["creators"]), 1)
            self.assertEqual(state["inaccessible"][0]["reason"], "HTTP 403")
            self.assertEqual(state["passes"]["leanpub"], {**state["passes"]["leanpub"], "status": "done",
                                                          "note": "Leanpub was productive."})
            self.assertEqual(state["usage"]["web_search_requests"], 3)

            # A paused turn is sent back as-is, with no extra user message.
            self.assertEqual(fake.calls[1]["messages"][-1]["role"], "assistant")
            results = fake.calls[2]["messages"][-1]["content"]
            self.assertEqual([r["tool_use_id"] for r in results], ["t1", "t2", "t3"])
            self.assertTrue(results[1]["is_error"])
            self.assertNotIn("is_error", results[0])

            discover.export(state, out, target=1)
            code = discover.main(["validate", "--out", tmp, "--target", "1"])
            self.assertEqual(code, 0)
            self.assertTrue((out / "summary.md").read_text().startswith("# Prospect list summary"))


if __name__ == "__main__":
    unittest.main()
