#!/usr/bin/env python3
"""Print a cover for every book in site/library.json that has no scanned one.

Each cover is cloth in the colour of the book's first genre, with the title, the author and a
small gilt emblem for the genre. It is drawn as a web page in headless Chromium, so the type
matches the site, and saved like the scans: site/covers/{id}.webp, 300 px wide.

  uv run --with playwright --with pillow scripts/make_covers.py           # books still without one
  uv run --with playwright --with pillow scripts/make_covers.py --redo    # also redraw the ones made before
  uv run --with playwright --with pillow scripts/make_covers.py --sample  # one per genre in /tmp/dripbook-covers

Then rerun scripts/build_library.py so library.json marks them as having a cover.
The ids of printed covers are kept in scripts/made_covers.txt, so --redo never touches a scan.
"""
import argparse
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "site" / "library.json"
COVERS = ROOT / "site" / "covers"
MADE = Path(__file__).resolve().parent / "made_covers.txt"
WIDTH, HEIGHT = 600, 900  # drawn at 2x, saved at 300 px wide

# Cloth colour and emblem for each genre. Books with no genre get "Literature".
STYLE = {
    "Philosophy": ("#27355A", "hourglass"),
    "History": ("#6E3326", "column"),
    "Romance": ("#7E2944", "rings"),
    "Poetry": ("#3B5640", "quill"),
    "Self-improvement": ("#8C5522", "peak"),
    "Myths & legends": ("#4B2E5E", "moon"),
    "Sci-fi & fantasy": ("#1D5A63", "planet"),
    "Mystery & crime": ("#26323F", "lens"),
    "Adventure": ("#2C5536", "compass"),
    "Biography & memoir": ("#58432F", "portrait"),
    "Plays": ("#7A2230", "mask"),
    "Funny books": ("#8E6A1F", "hat"),
    "Short stories": ("#2F4D68", "inkwell"),
    "Children's classics": ("#28678A", "boat"),
    "Horror & ghost stories": ("#2A2A33", "candle"),
    "Science & technology": ("#1F4973", "atom"),
    "Spirituality": ("#55447A", "sun"),
    "Business & money": ("#2B5949", "coins"),
    "Politics & power": ("#5C2828", "scales"),
    "Travel": ("#2D6464", "globe"),
    "Literature": ("#1E3A66", "book"),
}

# Emblems on a 100 x 100 grid, drawn as gilt lines.
EMBLEMS = {
    "hourglass": '<path d="M30 14h40M30 86h40M34 14c0 22 32 26 32 36S34 64 34 86M66 14c0 22-32 26-32 36s32 14 32 36"/><path d="M42 78h16l-8-10z" class="f"/>',
    "column": '<path d="M24 18h52M28 26h44M30 26v54M40 26v54M50 26v54M60 26v54M70 26v54M26 80h48M20 88h60M28 18q0-6 6-6h32q6 0 6 6"/>',
    "rings": '<circle cx="38" cy="54" r="22"/><circle cx="62" cy="54" r="22"/><path d="M38 32l4-8h-8zM62 32l4-8h-8z" class="f"/>',
    "quill": '<path d="M78 12C50 18 30 42 24 86M78 12c-6 26-22 46-44 56M78 12c-20 10-36 26-46 46M36 70l-6 10"/>',
    "peak": '<path d="M10 82l30-44 14 18 10-12 26 38zM40 38V16l14 6-14 6"/>',
    "moon": '<path d="M62 16a34 34 0 1 0 22 52A28 28 0 1 1 62 16z"/><path d="M28 28l2 6 6 2-6 2-2 6-2-6-6-2 6-2z" class="f"/>',
    "planet": '<circle cx="50" cy="50" r="22"/><ellipse cx="50" cy="50" rx="42" ry="12" transform="rotate(-18 50 50)"/><circle cx="82" cy="20" r="2.5" class="f"/><circle cx="18" cy="84" r="2" class="f"/>',
    "lens": '<circle cx="42" cy="42" r="24"/><path d="M60 60l24 24M30 34q4-8 12-10"/>',
    "compass": '<circle cx="50" cy="50" r="36"/><path d="M50 8v84M8 50h84"/><path d="M50 22l8 28-8 28-8-28z" class="f"/>',
    "portrait": '<ellipse cx="50" cy="50" rx="30" ry="38"/><circle cx="50" cy="40" r="11"/><path d="M28 78q4-22 22-22t22 22"/>',
    "mask": '<path d="M20 22q30-10 60 0v26q0 34-30 40Q20 82 20 48z"/><path d="M32 42q6-6 12 0M56 42q6-6 12 0M36 64q14 10 28 0"/>',
    "hat": '<path d="M32 66V24h36v42M14 70q36 10 72 0M14 70q2-6 18-4h36q16-2 18 4M32 56h36"/>',
    "inkwell": '<path d="M26 54h48v30H26zM34 54v-8h32v8M50 46L80 10"/><path d="M80 10l-6 14-4-4z" class="f"/>',
    "boat": '<path d="M14 62h72L72 80H28zM50 62V14l26 40H50M50 22L30 54h20"/>',
    "candle": '<path d="M38 46h24v40H38zM30 86h40M50 46v-6"/><path d="M50 14c8 10 8 18 0 24-8-6-8-14 0-24z" class="f"/>',
    "atom": '<ellipse cx="50" cy="50" rx="40" ry="14"/><ellipse cx="50" cy="50" rx="40" ry="14" transform="rotate(60 50 50)"/><ellipse cx="50" cy="50" rx="40" ry="14" transform="rotate(-60 50 50)"/><circle cx="50" cy="50" r="5" class="f"/>',
    "sun": '<circle cx="50" cy="50" r="16"/><path d="M50 10v14M50 76v14M10 50h14M76 50h14M22 22l10 10M68 68l10 10M78 22L68 32M32 68L22 78"/>',
    "coins": '<ellipse cx="50" cy="30" rx="26" ry="8"/><path d="M24 30v12c0 4 12 8 26 8s26-4 26-8V30M24 42v12c0 4 12 8 26 8s26-4 26-8V42M24 54v12c0 4 12 8 26 8s26-4 26-8V54"/>',
    "scales": '<path d="M50 14v70M30 84h40M18 26h64M50 14l-4 6h8z"/><path d="M18 26L8 54h20zM82 26L72 54h20zM8 54q10 10 20 0M72 54q10 10 20 0"/>',
    "globe": '<circle cx="50" cy="50" r="36"/><ellipse cx="50" cy="50" rx="16" ry="36"/><path d="M14 50h72M20 32h60M20 68h60"/>',
    "book": '<path d="M50 28C40 20 24 18 12 20v56c12-2 28 0 38 8 10-8 26-10 38-8V20c-12-2-28 0-38 8zM50 28v56"/>',
}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@500&family=Literata:ital,opsz,wght@0,7..72,500..700;1,7..72,400..500&display=block">
<style>
html,body{margin:0;background:#000}
#c{position:relative;width:%(w)dpx;height:%(h)dpx;overflow:hidden;color:#EAD9A2;background-color:var(--c);
  background-image:linear-gradient(90deg,rgba(0,0,0,.34),rgba(0,0,0,.08) 5%%,rgba(255,255,255,.07) 6%%,transparent 9%%),
    radial-gradient(120%% 80%% at 50%% 30%%,rgba(255,255,255,.07),transparent 60%%),
    repeating-linear-gradient(45deg,rgba(255,255,255,.028) 0 2px,transparent 2px 5px),
    repeating-linear-gradient(-45deg,rgba(0,0,0,.05) 0 2px,transparent 2px 5px)}
.frame{position:absolute;inset:30px 30px 30px 46px;border:2px solid rgba(234,217,162,.55);outline:1px solid rgba(234,217,162,.3);outline-offset:8px}
.in{position:absolute;inset:66px 64px 60px 82px;display:flex;flex-direction:column;align-items:center;text-align:center}
h1{margin:0;font:650 64px/1.08 Literata,serif;letter-spacing:-.01em;text-wrap:balance;hyphens:manual;color:#F3E6BC}
.rule{width:64px;height:2px;background:rgba(234,217,162,.7);margin:30px 0 22px}
.by{font:italic 450 30px/1.25 Literata,serif;color:#EAD9A2;text-wrap:balance}
.em{flex:1;min-height:0;width:100%%;display:flex;align-items:center;justify-content:center;padding-top:16px}
.em svg{width:210px;height:210px;max-height:100%%;fill:none;stroke:#EAD9A2;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.em .f{fill:#EAD9A2;stroke:none}
.mark{margin-top:26px;font:500 19px/1 'Hanken Grotesk',sans-serif;letter-spacing:.04em;color:rgba(234,217,162,.75);display:flex;gap:8px;align-items:center}
.mark i{display:block;width:9px;height:13px;background:#F2B230;border-radius:50%% 50%% 50%% 50%%/60%% 60%% 40%% 40%%}
</style></head><body>
<div id="c"><div class="frame"></div><div class="in">
<h1 id="t"></h1><div class="rule"></div><div class="by" id="a"></div>
<div class="em" id="e"></div><div class="mark"><i></i>Dripbook</div></div></div>
<script>
function draw(d){
  var c=document.getElementById('c'), t=document.getElementById('t');
  c.style.setProperty('--c', d.color);
  t.textContent=d.title; document.getElementById('a').textContent=d.author;
  document.getElementById('e').innerHTML='<svg viewBox="0 0 100 100">'+d.emblem+'</svg>';
  // Largest title size that fits in the upper part of the cover without breaking a word.
  var size=72; t.style.fontSize=size+'px';
  while(size>30 && (t.scrollWidth>t.clientWidth+1 || t.offsetHeight>390)){size-=2;t.style.fontSize=size+'px';}
}
</script></body></html>"""


def shade(hex_color, amount):
    """Lighten (amount > 0) or darken a #RRGGBB colour a little."""
    rgb = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    rgb = [max(0, min(255, round(v + (255 - v if amount > 0 else v) * amount))) for v in rgb]
    return "#" + "".join(f"{v:02X}" for v in rgb)


def style_for(book_id, book):
    genre = next((g for g in book.get("genres", []) if g in STYLE), "Literature")
    color, emblem = STYLE[genre]
    # Books next to each other in a row shouldn't be the exact same cloth.
    return shade(color, ((book_id * 7) % 5 - 2) * 0.04), EMBLEMS[emblem]


def chromium():
    for path in sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"), reverse=True):
        return str(path)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redo", action="store_true", help="redraw covers this script made before")
    ap.add_argument("--sample", action="store_true", help="draw one cover per genre into --out instead")
    ap.add_argument("--out", default="/tmp/dripbook-covers", help="where --sample writes (default %(default)s)")
    args = ap.parse_args()

    from PIL import Image
    from playwright.sync_api import sync_playwright

    lib = json.loads(LIBRARY.read_text(encoding="utf-8"))
    made = set(MADE.read_text().split()) if MADE.exists() else set()
    todo = [i for i in lib["order"]
            if not (COVERS / f"{i}.webp").exists() or (args.redo and str(i) in made)]
    out_dir = COVERS
    if args.sample:
        seen, pick = set(), []
        for i in todo:  # one book per emblem, so every style gets a look
            key = style_for(i, lib["books"][str(i)])[1]
            if key not in seen:
                seen.add(key)
                pick.append(i)
        todo = pick
        out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{len(todo)} covers to draw")

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium())
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        page.set_content(PAGE % {"w": WIDTH, "h": HEIGHT}, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        for n, book_id in enumerate(todo, 1):
            book = lib["books"][str(book_id)]
            color, emblem = style_for(book_id, book)
            page.evaluate("d => draw(d)", {"title": book["title"], "author": book.get("author", ""),
                                           "color": color, "emblem": emblem})
            png = page.locator("#c").screenshot()
            image = Image.open(io.BytesIO(png)).convert("RGB")
            image = image.resize((WIDTH // 2, HEIGHT // 2), Image.LANCZOS)
            image.save(out_dir / f"{book_id}.webp", "WEBP", quality=80, method=6)
            made.add(str(book_id))
            if n % 50 == 0:
                print(f"  {n}/{len(todo)}")
        browser.close()

    if not args.sample:
        MADE.write_text("\n".join(sorted(made, key=int)) + "\n")
    print(f"done: {out_dir}")


if __name__ == "__main__":
    main()
