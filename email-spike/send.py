#!/usr/bin/env python3
"""Send one lesson through Postmark, to check the account, domain and stream work.

Standard library only. Lessons go out on a Broadcast stream, where Postmark adds
the unsubscribe link and the List-Unsubscribe headers for us.

  python3 email-spike/send.py --to you@dripbook.ai                     # built-in sample lesson
  python3 email-spike/send.py --to you@dripbook.ai --html lesson-1.html --subject "Day 1"
  python3 email-spike/send.py --to you@dripbook.ai --dry-run           # Postmark checks it, sends nothing

Needs POSTMARK_SERVER_TOKEN (Postmark: Servers → your server → API Tokens).
--dry-run uses Postmark's POSTMARK_API_TEST token instead, so it needs no account.
Until Postmark approves the account, it only delivers to addresses on your own
domain, so send the first tests to an @dripbook.ai address.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.postmarkapp.com/email"
TEST_TOKEN = "POSTMARK_API_TEST"

SAMPLE = """<!doctype html>
<html><body style="margin:0;padding:24px;font-family:Georgia,serif;font-size:18px;line-height:1.6;color:#222">
<div style="max-width:560px;margin:0 auto">
<p style="font-size:14px;color:#777">The Art of War, lesson 1 of 13</p>
<h1 style="font-size:24px">Laying plans</h1>
<p>Sun Tzu said: The art of war is of vital importance to the State.</p>
<p>It is a matter of life and death, a road either to safety or to ruin.
Hence it is a subject of inquiry which can on no account be neglected.</p>
<p style="font-size:14px;color:#777">Tomorrow: waging war.</p>
</div></body></html>"""


def text_version(html):
    """A plain-text part for the HTML, so the email also reads in text-only clients."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</(p|h[1-6]|li|div)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def build_message(args, html):
    return {
        "From": args.sender,
        "To": args.to,
        "Subject": args.subject,
        "HtmlBody": html,
        "TextBody": text_version(html),
        "MessageStream": args.stream,
        "Tag": "spike",
    }


def send(message, token):
    req = urllib.request.Request(
        API,
        data=json.dumps(message).encode(),
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json", "X-Postmark-Server-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        with e:
            return json.load(e)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--to", required=True, help="recipient address")
    p.add_argument("--sender", default="Dripbook <lessons@mail.dripbook.ai>", help="From address; its domain must be verified in Postmark")
    p.add_argument("--subject", default="The Art of War, day 1: Laying plans")
    p.add_argument("--html", type=Path, help="lesson HTML file (default: a built-in sample)")
    p.add_argument("--stream", default="broadcast", help="Postmark message stream ID (default: broadcast)")
    p.add_argument("--dry-run", action="store_true", help="validate with Postmark's test token; nothing is delivered")
    args = p.parse_args(argv)

    token = TEST_TOKEN if args.dry_run else os.environ.get("POSTMARK_SERVER_TOKEN", "")
    if not token:
        sys.exit("Set POSTMARK_SERVER_TOKEN, or use --dry-run.")
    html = args.html.read_text() if args.html else SAMPLE
    if len(html.encode()) > 100_000:
        print(f"Warning: the HTML is {len(html.encode()) // 1000} KB. Gmail cuts off emails over about 100 KB.", file=sys.stderr)

    result = send(build_message(args, html), token)
    if result.get("ErrorCode", 1) != 0:
        sys.exit(f"Postmark refused it (error {result.get('ErrorCode')}): {result.get('Message')}")
    what = "Postmark accepted the test request; nothing was delivered" if args.dry_run else "Sent"
    print(f"{what}. MessageID {result.get('MessageID')} to {result.get('To')}.")


if __name__ == "__main__":
    main()
