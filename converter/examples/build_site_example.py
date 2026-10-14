#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdfium2>=4", "pillow>=10"]
# ///
"""Build site/example/, the public example course linked from the landing page.

  uv run converter/convert.py converter/examples/masteringclaudecode-sample.pdf \
      --plan converter/examples/masteringclaudecode.plan.json --cta-url "../#start"
  uv run converter/examples/build_site_example.py

Takes the converter's output for the "Claude Code: From Basics to Mastery" sample,
replaces the Day 3 comparison table (which PDF extraction scrambles) with a real
table, turns the private preview into a public example page with the book's cover
and attribution, and writes it to site/example/.
"""
import html
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "converter/examples/masteringclaudecode-sample.pdf"
SRC = ROOT / "converter/output/claude-code-from-basics-to-mastery"
OUT = ROOT / "site/example"
BOOK_URL = "https://leanpub.com/masteringclaudecode"

# The table on page 18 of the sample, word for word.
HEAD = ["Dimension", "Claude Code", "GitHub Copilot", "Cursor"]
ROWS = [
    ["Primary interface", "Terminal + IDE extensions", "IDE plugins (10+ editors)", "AI-native IDE (VS Code fork)"],
    ["Agentic capability", "Full multi-step autonomy", "Inline suggestions + workspace agents", "Multi-file edits, cloud agents"],
    ["Model options", "Anthropic models only", "OpenAI, Anthropic, Google, xAI", "Multiple model providers"],
    ["External tool integration", "MCP servers (open standard)", "GitHub ecosystem extensions", "Cursor-specific extensions"],
    ["Slack integration", "Native async coding workflow", "Limited", "None"],
    ["Enterprise security", "HIPAA-ready, SSO, RBAC", "SAML SSO, IP indemnity", "SOC 2 Type 2, zero data retention"],
    ["Individual pricing", "$20/mo (Pro)", "$10/mo (Pro)", "$20/mo (Pro)"],
]
INTRO = "The comparison table below summarizes the key differences:"
AFTER = "The choice between these tools"


def table_html():
    cell = "padding:8px 12px 8px 0;text-align:left;vertical-align:top"
    head = "".join(f'<th class="dm-ink dm-rule" style="{cell};border-bottom:2px solid #141B33;font-weight:700">'
                   f"{html.escape(h)}</th>" for h in HEAD)
    body = "".join(
        "<tr>" + "".join(f'<td class="dm-ink dm-rule" style="{cell};border-bottom:1px solid #CDD4E2'
                         f'{";font-weight:600" if i == 0 else ""}">{html.escape(c)}</td>' for i, c in enumerate(row))
        + "</tr>" for row in ROWS)
    return ('<div style="overflow-x:auto;margin:0 0 18px">'
            '<table class="dm-ink" style="width:100%;min-width:480px;border-collapse:collapse;'
            "font-family:'Schibsted Grotesk',Helvetica,Arial,sans-serif;font-size:15px;line-height:1.4;color:#141B33\">"
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>\n")


def table_text():
    return "".join(f"{row[0]}\n" + "".join(f"  {HEAD[i]}: {c}\n" for i, c in enumerate(row) if i) + "\n"
                   for row in ROWS)


def fix_email(day3):
    start = INTRO + "</p>\n"
    i = day3.index(start) + len(start)
    j = day3.rindex("<p", i, day3.index(AFTER, i))  # the <p> that opens "The choice between..."
    return day3[:i] + table_html() + day3[j:]


def main():
    preview = (SRC / "preview.html").read_text(encoding="utf-8")
    day3 = (SRC / "day-03.html").read_text(encoding="utf-8")
    fixed = fix_email(day3)
    escaped = html.escape(day3)
    assert escaped in preview, "preview.html doesn't embed day-03.html as expected"
    page = preview.replace(escaped, html.escape(fixed))

    # Public example, not a private preview.
    swaps = [
        ('<span class="tag">Private preview</span>', '<span class="tag">Example course</span>'),
        ('<span class="eyebrow">Email course preview</span>', '<span class="eyebrow">Example Dripbook course</span>'),
        ("<footer><div class=\"wrap\">Made with Dripbook. This page is private and not listed anywhere.</div></footer>",
         f'<footer><div class="wrap">An example made with Dripbook from the free sample of '
         f'<a href="{BOOK_URL}">Claude Code: From Basics to Mastery</a>. Sample text and cover &copy; 2026 Steve Publications. '
         '<a href="../">Back to Dripbook</a></div></footer>'),
        ("<h1>Claude Code: From Basics to Mastery</h1>",
         '<div class="book"><img src="cover.jpg" width="160" height="229" alt="Book cover: Claude Code, From Basics to Mastery">'
         "<div><h1>Claude Code: From Basics to Mastery</h1>"),
        ("Readers get the first 3 free, then pay to unlock the rest.</p>",
         f'Readers get the first 3 free, then pay to unlock the rest. Get the full book on <a href="{BOOK_URL}">Leanpub</a>.</p></div></div>'),
        ("</style>", ".book{display:flex;flex-wrap:wrap;gap:32px;align-items:center}"
                     ".book img{border-radius:6px;box-shadow:0 8px 24px rgba(20,27,51,.25);flex:0 0 auto}"
                     ".book>div{flex:1 1 320px;min-width:0}</style>"),
    ]
    swaps.append(("as 7 daily lessons from pages 1–68.", "as 7 daily lessons made from its free sample."))
    for old, new in swaps:
        assert page.count(old) == 1, old[:60]
        page = page.replace(old, new)

    # The emails' unsubscribe link is filled in by whatever sends them; here it goes nowhere.
    page = page.replace(html.escape("{{UNSUBSCRIBE_URL}}"), "#").replace("{{UNSUBSCRIBE_URL}}", "#")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(page, encoding="utf-8")
    cover = pdfium.PdfDocument(str(PDF))[0].render(scale=1).to_pil().convert("RGB")
    cover.thumbnail((320, 460))
    cover.save(OUT / "cover.jpg", quality=85, optimize=True)

    # Keep the converter output consistent with the published page.
    (SRC / "day-03.html").write_text(fixed, encoding="utf-8")
    txt = (SRC / "day-03.txt").read_text(encoding="utf-8")
    i = txt.index(INTRO + "\n\n") + len(INTRO) + 2
    j = txt.index(AFTER, i)
    (SRC / "day-03.txt").write_text(txt[:i] + table_text() + txt[j:], encoding="utf-8")
    print(f"Wrote {OUT / 'index.html'} and {OUT / 'cover.jpg'}")


if __name__ == "__main__":
    main()
