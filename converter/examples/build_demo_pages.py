#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdfium2>=4", "pillow>=10"]
# ///
"""Build the demo pages. All are noindex and nothing on the public site links to them.

  site/think-python/                              Think Python course sales page, for readers
  site/think-python-preview/                      Think Python course preview, for the author
  site/demo-0105d285ea7c/                         index of all demo pages (unguessable folder)
  site/demo-0105d285ea7c/claude-code-preview/     example course from a Leanpub sample; kept in
                                                  the hidden folder until the author agrees

Render the converter output first, then run this script:

  uv run converter/convert.py converter/examples/masteringclaudecode-sample.pdf \
      --plan converter/examples/masteringclaudecode.plan.json --cta-url "/#start"
  uv run converter/convert.py converter/output/thinkpython2.pdf --pages 23-38 \
      --plan converter/examples/think-python.plan.json --cta-url "../think-python/#buy"
  uv run converter/examples/build_demo_pages.py

thinkpython2.pdf comes from https://greenteapress.com/thinkpython2/thinkpython2.pdf.
"""
import html
import shutil
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "site/demo-0105d285ea7c"
OUTPUT = ROOT / "converter/output"

CC_PDF = ROOT / "converter/examples/masteringclaudecode-sample.pdf"
CC_SRC = OUTPUT / "claude-code-from-basics-to-mastery"
CC_URL = "https://leanpub.com/masteringclaudecode"

TP_SRC = OUTPUT / "think-python"
TP_URL = "https://greenteapress.com/wp/think-python-2e/"
TP_PRICE = 49
# Shown next to the hero buy button. Only put a number here once real reader data backs it.
TP_BADGE = "Everything unlocked"
# Lessons per chapter: chapters 1 and 2 follow think-python.plan.json; the rest are
# estimated from each chapter's word count at about 900 words (4 minutes) a lesson.
TP_OUTLINE = [
    ("The way of the program", 3), ("Variables, expressions and statements", 3), ("Functions", 4),
    ("Case study: interface design", 3), ("Conditionals and recursion", 4), ("Fruitful functions", 4),
    ("Iteration", 3), ("Strings", 3), ("Case study: word play", 3), ("Lists", 4), ("Dictionaries", 4),
    ("Tuples", 3), ("Case study: data structure selection", 4), ("Files", 4), ("Classes and objects", 2),
    ("Classes and functions", 2), ("Classes and methods", 3), ("Inheritance", 4), ("The Goodies", 3),
    ("Appendix A: Debugging", 4), ("Appendix B: Analysis of algorithms", 4),
]

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


def fix_email(day3):
    start = INTRO + "</p>\n"
    i = day3.index(start) + len(start)
    j = day3.rindex("<p", i, day3.index(AFTER, i))  # the <p> that opens "The choice between..."
    return day3[:i] + table_html() + day3[j:]



PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>@TITLE@</title>
<meta name="theme-color" content="#141B33">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&family=Schibsted+Grotesk:wght@400;500;600;700;800&display=swap">
<link rel="stylesheet" href="/styles.css">
"""

INDEX_PAGE = PAGE_HEAD.replace("@TITLE@", "Dripbook demos") + """</head>
<body>
<main class="wrap page">
  <p class="eyebrow">Not listed anywhere</p>
  <h1>Dripbook demos</h1>
  <p>Pages for showing people what Dripbook makes. Share them by link only.</p>
  <ul>
@LINKS@
  </ul>
</main>
</body>
</html>
"""

SALES_PAGE = PAGE_HEAD.replace("@TITLE@", "Think Python, as a daily email course") + """<style>
.cover{position:relative;width:min(300px,78vw);aspect-ratio:2/3;margin-inline:auto;background:var(--band);color:var(--band-text);border-radius:10px 16px 16px 10px;box-shadow:0 24px 48px rgba(20,27,51,.28);padding:36px 28px;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden}
.cover::before{content:"";position:absolute;inset:0 auto 0 0;width:12px;background:rgba(0,0,0,.25)}
.cover .ribbon{right:28px;top:-6px;height:110px;width:26px}
.cover-t{margin:0;font-family:var(--serif);font-size:44px;line-height:1.05}
.cover-s{margin:10px 0 0;font-family:var(--serif);font-style:italic;font-size:19px;color:var(--band-muted)}
.cover-a{margin:0;font-size:15px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--band-muted)}
.buy-top{display:flex;flex-direction:column;gap:12px}
.buy-row{display:flex;flex-wrap:wrap;align-items:center;gap:14px}
.buy-top .btn{border:0;cursor:pointer;font:inherit;font-size:19px;font-weight:700}
.badge{font-family:var(--sans);display:inline-flex;align-items:center;padding:8px 14px;border-radius:999px;background:var(--ribbon-soft);border:1.5px solid var(--ribbon);font-size:14px;font-weight:800;color:var(--ink)}
.or{display:flex;align-items:center;gap:14px;max-width:34em;margin:0;font-size:14px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.or::before,.or::after{content:"";flex:1;height:1.5px;background:var(--line)}
.join{display:flex;gap:10px;max-width:36em}
.join input{flex:1 1 auto;min-width:0;width:100%;min-height:60px;border-radius:999px;border:1.5px solid var(--rule);background:var(--surface);color:var(--ink);font:inherit;font-size:18px;padding:0 24px}
.join .btn{border:0;cursor:pointer;font:inherit;font-size:19px;font-weight:700}
.demo-note{margin:0;font-size:16px;font-weight:600;color:var(--ink)}
.facts{display:flex;flex-wrap:wrap;gap:12px 28px;margin:0;padding:0;list-style:none;font-size:16px;font-weight:600;color:var(--muted)}
.facts li::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ribbon);margin-right:10px;vertical-align:middle}
.lesson-frame{border:1.5px solid var(--rule);border-radius:20px;overflow:hidden;background:#F4F5F8;max-width:760px}
.lesson-frame iframe{display:block;width:100%;height:760px;border:0}
.outline{max-width:860px}
.outline .row-locked .day{background:var(--ink);color:var(--paper)}
.outline .row-locked{background:transparent}
.outline .inbox-head+.row{background:var(--ribbon-soft)}
.outline .inbox-head+.row .day{background:var(--ribbon);color:var(--on-ribbon)}
.outline .day{min-width:92px;text-align:center}
.buy .btn{border:0;cursor:pointer;font:inherit;font-size:19px;font-weight:700;align-self:flex-start}
.buy .price{margin:0;font-size:clamp(56px,7vw,88px);font-weight:800;letter-spacing:-.03em;line-height:1}
.buy ul{margin:0;padding-left:22px;display:flex;flex-direction:column;gap:8px;font-size:18px;line-height:1.5;color:var(--band-muted)}
.buy .demo-note{color:var(--ribbon)}
</style>
</head>
<body>

<header class="wrap nav">
  <span class="brand">Think Python <span style="font-weight:500;color:var(--muted)">· email course</span></span>
  <nav aria-label="Main">
    <a href="#outline">What you learn</a>
    <a href="#faq">Questions</a>
    <a href="#buy" class="btn">Get the course</a>
  </nav>
</header>

<main>
  <section class="wrap hero split">
    <div class="hero-copy stack">
      <p class="eyebrow">A daily email course</p>
      <h1>Learn Python, one short lesson a day.</h1>
      <p class="lede">Allen B. Downey's <em>Think Python</em>, in @TOTAL@ lessons of about four minutes each. One arrives every morning, and you can read ahead whenever you are hooked.</p>
      <div class="buy-top">
        <div class="buy-row">
          <button type="button" class="btn btn-ribbon btn-lg" data-demo data-note="note-buy-top">Get all @TOTAL@ lessons for $@PRICE@</button>
          <span class="badge">@BADGE@</span>
        </div>
        <p class="small">One payment. Every lesson unlocked from day one, read at your own pace.</p>
        <p class="demo-note" id="note-buy-top" role="status" aria-live="polite"></p>
      </div>
      <p class="or">or try it first</p>
      <form class="join" data-demo data-note="note-free">
        <label for="email" class="sr" style="position:absolute;left:-10000px">Your email</label>
        <input id="email" type="email" placeholder="Your email" autocomplete="email" required>
        <button type="submit" class="btn" style="flex:0 0 auto">Start free</button>
      </form>
      <p class="demo-note" id="note-free" role="status" aria-live="polite"></p>
      <ul class="facts">
        <li>Pause any time</li>
        <li>Read ahead whenever you like</li>
      </ul>
    </div>
    <div class="hero-visual" style="flex:1 1 360px">
      <div class="cover" role="img" aria-label="Think Python by Allen B. Downey">
        <div class="ribbon" aria-hidden="true"></div>
        <div><p class="cover-t">Think Python</p><p class="cover-s">How to Think Like a Computer Scientist</p></div>
        <p class="cover-a">Allen B. Downey</p>
      </div>
    </div>
  </section>

  <section class="statement">
    <div class="wrap section split">
      <p class="big">No prior programming needed. Just your inbox and ten minutes with a Python prompt open.</p>
      <p class="aside">Each lesson is a passage of the book itself, cut where a lesson should end. Type the examples as you go: by the end you write functions, work with lists and dictionaries, read files and build your own classes.</p>
    </div>
  </section>

  <section class="wrap section stack" style="gap:40px">
    <div class="stack" style="gap:16px">
      <p class="eyebrow">Lesson 1, as it arrives</p>
      <h2>See exactly what you get.</h2>
    </div>
    <div class="lesson-frame"><iframe src="lesson-1.html" title="Lesson 1 email" loading="lazy"></iframe></div>
    <p class="small">Scroll inside the box to read the whole lesson.</p>
  </section>

  <section id="outline" class="wrap section stack outline" style="gap:40px">
    <div class="stack" style="gap:16px">
      <p class="eyebrow">What you learn</p>
      <h2>@TOTAL@ lessons, the whole book.</h2>
      <p class="body">Chapter by chapter, in the author's order. Lesson counts after Chapter 2 are approximate.</p>
    </div>
    <div class="inbox">
      <div class="inbox-head"><span>@TOTAL@ lessons, one a day</span></div>
@OUTLINE@
    </div>
  </section>

  <section id="buy" class="wrap" style="padding-bottom:112px">
    <div class="cta buy">
      <div class="stack" style="gap:20px">
        <p class="eyebrow" style="color:var(--band-muted)">The full course</p>
        <p class="price">$@PRICE@</p>
        <p class="body">One payment for all @TOTAL@ lessons. Start with the three free ones if you want to try it first.</p>
      </div>
      <div class="stack" style="gap:24px">
        <ul>
          <li>A lesson every morning, about four minutes each</li>
          <li>"Send the next one now" whenever you want more</li>
          <li>Pause for a busy week and pick up where you left off</li>
          <li>Every code example and exercise from the book</li>
        </ul>
        <button type="button" class="btn btn-ribbon btn-lg" data-demo data-note="note-buy">Buy the course for $@PRICE@</button>
        <p class="demo-note" id="note-buy" role="status" aria-live="polite"></p>
      </div>
    </div>
  </section>

  <section id="faq" class="wrap section stack faq" style="gap:32px;padding-top:0">
    <h2>Questions</h2>
    <div class="faq-list">
      <details><summary>Do I need to know how to program?</summary><p>No. The book starts from what a program is and how to run one. You need a computer where you can install Python, or a browser to use Python online.</p></details>
      <details><summary>What happens after the free lessons?</summary><p>The third lesson ends with a link to buy the rest. If you don't, the emails simply stop.</p></details>
      <details><summary>Can I read faster than one a day?</summary><p>Yes. Every lesson has a "Send the next one now" button.</p></details>
      <details><summary>Can I pause?</summary><p>Yes, from a link in any email. The course waits and picks up exactly where you left off.</p></details>
    </div>
  </section>
</main>

<footer>
  <div class="wrap foot">
    <span>Based on <a href="@BOOK_URL@">Think Python, 2nd edition</a> by Allen B. Downey, <a href="https://creativecommons.org/licenses/by-nc/3.0/">CC BY-NC 3.0</a>.</span>
    <span>Demo page made with Dripbook. Nothing here is for sale.</span>
  </div>
</footer>

<script>
document.querySelectorAll('[data-demo]').forEach(function (el) {
  var note = document.getElementById(el.dataset.note);
  el.addEventListener(el.tagName === 'FORM' ? 'submit' : 'click', function (e) {
    e.preventDefault();
    note.textContent = 'This is a demo page, so sign-up and checkout are not live.';
  });
});
</script>
</body>
</html>
"""

def apply(page, swaps):
    for old, new in swaps:
        assert page.count(old) == 1, old[:60]
        page = page.replace(old, new)
    # The emails' unsubscribe link is filled in by whatever sends them; here it goes nowhere.
    return page.replace(html.escape("{{UNSUBSCRIBE_URL}}"), "#").replace("{{UNSUBSCRIBE_URL}}", "#")


BOOK_CSS = (".book{display:flex;flex-wrap:wrap;gap:32px;align-items:center}"
            ".book img{border-radius:6px;box-shadow:0 8px 24px rgba(20,27,51,.25);flex:0 0 auto}"
            ".book>div{flex:1 1 320px;min-width:0}</style>")
PRIVATE_FOOTER = '<footer><div class="wrap">Made with Dripbook. This page is private and not listed anywhere.</div></footer>'


def build_claude_code_preview():
    out = DEMO / "claude-code-preview"
    preview = (CC_SRC / "preview.html").read_text(encoding="utf-8")
    day3 = (CC_SRC / "day-03.html").read_text(encoding="utf-8")
    fixed = fix_email(day3)
    assert html.escape(day3) in preview, "preview.html doesn't embed day-03.html as expected"
    page = apply(preview.replace(html.escape(day3), html.escape(fixed)), [
        ('<span class="tag">Private preview</span>', '<span class="tag">Example course</span>'),
        ('<span class="eyebrow">Email course preview</span>', '<span class="eyebrow">Example Dripbook course</span>'),
        (PRIVATE_FOOTER,
         f'<footer><div class="wrap">An example made with Dripbook from the free sample of '
         f'<a href="{CC_URL}">Claude Code: From Basics to Mastery</a>. Sample text and cover &copy; 2026 Steve Publications. '
         'Not for public listing.</div></footer>'),
        ("<h1>Claude Code: From Basics to Mastery</h1>",
         '<div class="book"><img src="cover.jpg" width="160" height="229" alt="Book cover: Claude Code, From Basics to Mastery">'
         "<div><h1>Claude Code: From Basics to Mastery</h1>"),
        ("Readers get the first 3 free, then pay to unlock the rest.</p>",
         f'Readers get the first 3 free, then pay to unlock the rest. Get the full book on <a href="{CC_URL}">Leanpub</a>.</p></div></div>'),
        ("as 7 daily lessons from pages 1–68.", "as 7 daily lessons made from its free sample."),
        ("</style>", BOOK_CSS),
    ])
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    cover = pdfium.PdfDocument(str(CC_PDF))[0].render(scale=1).to_pil().convert("RGB")
    cover.thumbnail((320, 460))
    cover.save(out / "cover.jpg", quality=85, optimize=True)

    return out


def build_think_python_preview():
    out = ROOT / "site/think-python-preview"
    page = apply((TP_SRC / "preview.html").read_text(encoding="utf-8"), [
        ('<span class="tag">Private preview</span>', '<span class="tag">Example course</span>'),
        ('<span class="eyebrow">Email course preview</span>', '<span class="eyebrow">Example Dripbook course</span>'),
        (PRIVATE_FOOTER,
         '<footer><div class="wrap">An example made with Dripbook from chapters 1 and 2 of '
         f'<a href="{TP_URL}">Think Python, 2nd edition</a>, by Allen B. Downey, licensed '
         '<a href="https://creativecommons.org/licenses/by-nc/3.0/">CC BY-NC 3.0</a>. '
         '<a href="../think-python/">See the course sales page</a>.</div></footer>'),
    ])
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    if (TP_SRC / "figures").is_dir():
        shutil.copytree(TP_SRC / "figures", out / "figures", dirs_exist_ok=True)
    return out


def build_think_python_sales():
    out = ROOT / "site/think-python"
    out.mkdir(parents=True, exist_ok=True)
    total = sum(n for _, n in TP_OUTLINE)
    lesson1 = (TP_SRC / "day-01.html").read_text(encoding="utf-8").replace("{{UNSUBSCRIBE_URL}}", "#")
    # The rendered plan covers chapters 1 and 2; the sales page sells the whole book.
    lesson1 = lesson1.replace("Lesson 1 of 6", f"Lesson 1 of {total}")
    (out / "lesson-1.html").write_text(lesson1, encoding="utf-8")
    rows, day = [], 1
    for i, (title, n) in enumerate(TP_OUTLINE):
        days = f"Day {day}" if n == 1 else f"Days {day}–{day + n - 1}"
        label = title if title.startswith("Appendix") else f"Chapter {i + 1}: {title}"
        free = day == 1
        rows.append(
            f'<div class="row{" row-locked" if not free else ""}"><span class="day">{days}</span>'
            f'<div class="row-text"><span class="subject">{html.escape(label)}</span>'
            f'<span class="preview">{n} lessons{", the first 3 free" if free else ""}</span></div></div>')
        day += n
    page = (SALES_PAGE.replace("@TOTAL@", str(total)).replace("@PRICE@", str(TP_PRICE))
            .replace("@OUTLINE@", "\n".join(rows)).replace("@BOOK_URL@", TP_URL).replace("@BADGE@", TP_BADGE))
    (out / "index.html").write_text(page, encoding="utf-8")
    return out


def build_index(pages):
    links = "\n".join(f'<li><a href="/{p.relative_to(ROOT / "site").as_posix()}/">{html.escape(title)}</a></li>'
                      for p, title in pages)
    (DEMO / "index.html").write_text(INDEX_PAGE.replace("@LINKS@", links), encoding="utf-8")


def main():
    pages = [
        (build_think_python_sales(), "Think Python: course sales page (what readers see)"),
        (build_think_python_preview(), "Think Python: course preview (what the author sees)"),
        (build_claude_code_preview(), "Claude Code: From Basics to Mastery: course preview"),
    ]
    build_index(pages)
    for p, _ in pages:
        print(f"Wrote {p.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
