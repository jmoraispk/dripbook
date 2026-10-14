#!/usr/bin/env python3
"""Pre-launch checks for the static site in site/.

  python3 scripts/site_check.py                 # report broken links and unfilled settings
  python3 scripts/site_check.py --strict        # also fail if any {{SETTING}} is unfilled
  python3 scripts/site_check.py fill SITE_URL=https://dripbook.co CONTACT_EMAIL=hi@dripbook.co

Settings are written in the pages as {{NAME}} and filled in place by `fill`:
  SITE_URL       public address of the site, no trailing slash (used for the share image)
  CONTACT_EMAIL  where the Contact link and privacy page point
  EXAMPLE_URL    the example course preview linked from the upload section
"""
import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "site"
SETTINGS = ("SITE_URL", "CONTACT_EMAIL", "EXAMPLE_URL")
TOKEN = re.compile(r"\{\{([A-Z_]+)\}\}")
TEXT_SUFFIXES = {".html", ".css", ".txt", ".svg", ".js", ".json", ".xml"}


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.refs = set(), []
        self.has_title = self.has_viewport = self.has_lang = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "html" and a.get("lang"):
            self.has_lang = True
        if tag == "title":
            self.has_title = True
        if tag == "meta" and a.get("name") == "viewport":
            self.has_viewport = True
        for attr in ("href", "src"):
            if a.get(attr):
                self.refs.append(a[attr])


def check(strict):
    problems, unfilled = [], {}
    for path in sorted(SITE.rglob("*")):
        if path.suffix not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(SITE)
        for name in TOKEN.findall(text):
            unfilled.setdefault(name, set()).add(str(rel))
        if path.suffix != ".html":
            continue
        page = Page()
        page.feed(text)
        for ok, what in ((page.has_title, "<title>"), (page.has_viewport, "viewport meta"), (page.has_lang, "<html lang>")):
            if not ok:
                problems.append(f"{rel}: missing {what}")
        for ref in page.refs:
            if TOKEN.search(ref) or re.match(r"^(https?:|mailto:|tel:|data:)", ref):
                continue
            target, _, anchor = ref.partition("#")
            if not target:
                if anchor and anchor not in page.ids:
                    problems.append(f"{rel}: link to #{anchor} has no matching id")
                continue
            local = SITE / target.lstrip("/") if target.startswith("/") else path.parent / target
            if local.is_dir():
                local = local / "index.html"
            if not local.exists():
                problems.append(f"{rel}: {ref} does not exist")

    for p in problems:
        print(f"error   {p}")
    for name, files in sorted(unfilled.items()):
        level = "error  " if strict else "warning"
        print(f"{level} {{{{{name}}}}} not filled in: {', '.join(sorted(files))}")
    failed = bool(problems) or (strict and bool(unfilled))
    if not problems and not unfilled:
        print("site/ looks ready.")
    elif not failed:
        print("\nNo broken links. Fill the settings above before launch, e.g.\n"
              "  python3 scripts/site_check.py fill SITE_URL=https://example.com CONTACT_EMAIL=you@example.com")
    return 1 if failed else 0


def fill(assignments):
    values = {}
    for item in assignments:
        name, sep, value = item.partition("=")
        if not sep or name not in SETTINGS or not value:
            sys.exit(f"Expected NAME=value with NAME one of {', '.join(SETTINGS)}; got {item!r}")
        values[name] = value.rstrip("/") if name == "SITE_URL" else value
    for path in sorted(SITE.rglob("*")):
        if path.suffix not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        new = TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), text)
        if new != text:
            path.write_text(new, encoding="utf-8")
            print(f"updated {path.relative_to(SITE)}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="check", choices=("check", "fill"))
    parser.add_argument("assignments", nargs="*", metavar="NAME=value")
    parser.add_argument("--strict", action="store_true", help="fail when a setting is still unfilled")
    args = parser.parse_args()
    if args.command == "fill":
        return fill(args.assignments)
    return check(args.strict)


if __name__ == "__main__":
    sys.exit(main())
