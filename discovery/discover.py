#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic>=1.13"]
# ///
"""Customer discovery for Dripbook: find ebook creators worth pitching.

Claude researches one source at a time (Leanpub, Gumroad, ...) with web search
and web fetch, follows the rules in brief.md, and saves each qualified creator
through a record_creator tool. State is saved after every step, so a run can be
stopped with Ctrl-C and resumed. Everything is written to discovery/output/,
which git ignores because it holds people's names and contact details.

  uv run discovery/discover.py run                   # research until the goal is met or sources run out
  uv run discovery/discover.py run --sources gumroad --target 20
  uv run discovery/discover.py run --dry-run         # print the prompts without calling the API
  uv run discovery/discover.py export                # rewrite prospects.csv and summary.md from saved state
  uv run discovery/discover.py check-links           # confirm the URLs in prospects.csv still load
  uv run discovery/discover.py validate [FILE.csv]   # check a CSV against the brief's format

`run` needs ANTHROPIC_API_KEY (or an `ant auth login` profile). The other
commands only use the standard library.
"""
import argparse
import csv
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIEF = HERE / "brief.md"
DEFAULT_OUT = HERE / "output"

COLUMNS = [
    "creator_name", "book_title", "book_url", "platform", "topic", "price_usd", "sample_url",
    "traction", "has_course", "last_active", "contact_channel", "fit_score", "notes",
]
PLATFORMS = ["Leanpub", "Gumroad", "Payhip", "Lemon Squeezy", "own site", "other"]
HAS_COURSE = ["yes", "no", "unclear"]

# One research pass per source, in the brief's order of expected yield.
SOURCES = {
    "leanpub": "Leanpub (leanpub.com). Browse the computer-programming categories and bestseller lists. "
               "Most books have a free sample, and book pages often show reader counts, update dates and "
               "the author's other books.",
    "gumroad": "Gumroad. Use Gumroad Discover and web searches like `site:gumroad.com rust ebook` or "
               "`site:gumroad.com \"system design\" pdf`. Product pages often show ratings and sales counts.",
    "payhip-lemonsqueezy": "Payhip and Lemon Squeezy. Use web searches like `site:payhip.com python ebook` "
                           "or `\"lemonsqueezy.com\" ebook golang`.",
    "newsletters": "Engineering newsletters on Substack and Beehiiv whose writers also sell a paid ebook. "
                   "Find newsletters that link to their own ebook, then verify the book on its sales page.",
    "social": "Developers promoting their own ebook on X and LinkedIn. Find them through web search only. "
              "You cannot open x.com or linkedin.com pages, so verify each creator on their sales page or "
              "personal site. A profile URL seen in search results is fine as a contact channel.",
}

# Pages that need a login to read. Search results from them are fine; fetching them is not.
NO_FETCH_DOMAINS = ["x.com", "twitter.com", "linkedin.com", "facebook.com", "instagram.com"]

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5"]

SYSTEM = """You are a careful market researcher building a prospect list for Dripbook. The brief below is your spec.

<brief>
{brief}
</brief>

How this run works:
- The work is split into passes, one per source. Each pass names its source and lists the creators already recorded.
- Use web_search to find candidates and web_fetch to read their public pages. Verify the brief's four requirements on public pages before you record anyone. For any other field you can't verify, leave it blank or write "unclear" as the brief says.
- Save each creator with record_creator as soon as you have verified them, one call per creator. Include the URLs of the pages you relied on.
- When a source or page can't be used (login wall, paywall, blocked, robots-disallowed, repeated errors), call report_inaccessible_source once for it and move on.
- traction_strength is only used for sorting: 5 means very strong public evidence (thousands of ratings, buyers or subscribers), 3 moderate (dozens of ratings, a few thousand followers), 1 thin but present.
- A shorter honest list is better than a padded one.
- When you finish a pass, reply with 2 to 4 plain sentences: how productive the source was, where it ran dry, and anything notable about pricing or the market. That note goes into the final summary."""

COMPACTION_INSTRUCTIONS = (
    "Summarize this research pass so it can continue without the earlier pages. Keep: the searches, categories "
    "and listings already covered; every creator already recorded; candidates checked and rejected, with the "
    "reason; candidates still to verify, with their URLs; and observations about pricing or the market."
)

RECORD_TOOL = {
    "name": "record_creator",
    "description": "Save one verified creator to the prospect list. Call once per creator, only after "
                   "checking them against the brief on public pages. The script rejects duplicates and "
                   "malformed fields and tells you what to fix.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "creator_name": {"type": "string", "description": "Person or brand name"},
            "book_title": {"type": "string", "description": "Title of the main ebook"},
            "book_url": {"type": "string", "description": "Sales page URL"},
            "platform": {"type": "string", "enum": PLATFORMS},
            "topic": {"type": "string", "description": "Short topic, e.g. 'Rust for Python developers'"},
            "price_usd": {"type": "string", "description": "Listed price such as '29' or '19-39'; empty if not shown"},
            "sample_url": {"type": "string", "description": "Free sample, preview or table of contents URL; empty if none"},
            "traction": {"type": "string", "description": "Public evidence of sales or audience, quoted or summarized with where it came from"},
            "has_course": {"type": "string", "enum": HAS_COURSE},
            "last_active": {"type": "string", "description": "Most recent visible activity as YYYY-MM-DD or YYYY-MM; empty if unknown"},
            "contact_channel": {"type": "string", "description": "Contact form URL, business email shown on their site, or public social profile URL"},
            "fit_score": {"type": "integer", "enum": [1, 2, 3]},
            "traction_strength": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "notes": {"type": "string", "description": "Useful details for a personal pitch; other books go here"},
            "evidence_urls": {"type": "array", "items": {"type": "string"}, "description": "Public pages you used to verify this row"},
        },
        "required": COLUMNS + ["traction_strength", "evidence_urls"],
        "additionalProperties": False,
    },
}

REPORT_TOOL = {
    "name": "report_inaccessible_source",
    "description": "Note a source or page you could not use, and why, for the final report.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Site or source name, e.g. 'Gumroad Discover'"},
            "url": {"type": "string"},
            "reason": {"type": "string", "description": "What blocked you, e.g. 'login required', 'HTTP 403'"},
        },
        "required": ["source", "url", "reason"],
        "additionalProperties": False,
    },
}

TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 20},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 20, "max_content_tokens": 25000,
     "blocked_domains": NO_FETCH_DOMAINS},
    RECORD_TOOL,
    REPORT_TOOL,
]


# ---------------------------------------------------------------- validation

URL = re.compile(r"^https?://[^\s/]+\.[^\s]+$", re.I)
EMAIL = re.compile(r"^(mailto:)?[^@\s]+@[^@\s]+\.[^@\s]+$", re.I)
PHONE = re.compile(r"^(tel:)?\+?[\d\s().-]{7,}$", re.I)
ACTIVE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def row_problems(row):
    """Problems with one row's CSV columns, as human-readable strings."""
    problems = []
    for col in ("creator_name", "book_title", "book_url", "topic", "traction", "contact_channel"):
        if not str(row.get(col, "")).strip():
            problems.append(f"{col} is required")
    for col in ("book_url", "sample_url"):
        value = row.get(col, "")
        if value and not URL.match(value):
            problems.append(f"{col} must be a full http(s) URL, got {value!r}")
    contact = row.get("contact_channel", "")
    if contact and PHONE.match(contact):
        problems.append("contact_channel looks like a phone number; use a contact form, business email or public profile")
    elif contact and not (URL.match(contact) or EMAIL.match(contact)):
        problems.append(f"contact_channel must be a URL or an email address, got {contact!r}")
    if row.get("platform") not in PLATFORMS:
        problems.append(f"platform must be one of {PLATFORMS}")
    if row.get("has_course") not in HAS_COURSE:
        problems.append(f"has_course must be one of {HAS_COURSE}")
    if row.get("last_active") and not ACTIVE.match(row["last_active"]):
        problems.append("last_active must be YYYY-MM-DD or YYYY-MM")
    if str(row.get("fit_score")) not in ("1", "2", "3"):
        problems.append("fit_score must be 1, 2 or 3")
    return problems


def name_key(name):
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def url_key(url):
    parts = urllib.parse.urlsplit(url.strip().lower())
    host = parts.netloc.removeprefix("www.")
    return host + parts.path.rstrip("/")


def clean_row(data, existing):
    """Normalize a record_creator input. Returns (row, problems)."""
    row = {k: (" ".join(v.split()) if isinstance(v, str) else v) for k, v in data.items()}
    problems = row_problems(row)
    if row.get("traction_strength") not in (1, 2, 3, 4, 5):
        problems.append("traction_strength must be 1 to 5")
    for i, other in enumerate(existing, 1):
        if name_key(other["creator_name"]) == name_key(row.get("creator_name", "")) \
                or url_key(other["book_url"]) == url_key(row.get("book_url", "")):
            problems.append(f"already on the list as #{i} ({other['creator_name']}, {other['book_url']}); "
                            "one row per creator, so mention extra books in that row's notes instead")
            break
    return row, problems


# --------------------------------------------------------------------- state

def load_state(out):
    path = out / "state.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"creators": [], "inaccessible": [], "passes": {}, "usage": {}}


def save_state(state, out):
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(out / "state.json")


def count_qualified(state):
    return sum(1 for c in state["creators"] if c["fit_score"] >= 2)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ research

def pass_prompt(source, state, target, today):
    on_list = "\n".join(f"- {c['creator_name']} ({c['book_url']})" for c in state["creators"]) or "(none yet)"
    qualified = count_qualified(state)
    year_ago = today - timedelta(days=365)
    return (
        f"Today is {today.isoformat()}, so \"active in the last 12 months\" means activity since {year_ago.isoformat()}.\n\n"
        f"This pass: {SOURCES[source]}\n\n"
        f"The list has {qualified} creators with fit_score 2 or 3 and the goal is {target}, so look for about "
        f"{max(target - qualified, 0)} more here. Record borderline creators too, with fit_score 1; they only "
        "make the final list if the goal can't be reached otherwise.\n\n"
        f"Already on the list, skip these:\n{on_list}\n\n"
        "Work through this source until you reach the goal or stop finding new qualified creators, then write your pass note."
    )


def tool_result(tool_use_id, text, error=False):
    result = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if error:
        result["is_error"] = True
    return result


def handle_tool(block, state, source, target):
    if block.name == "record_creator":
        row, problems = clean_row(dict(block.input), state["creators"])
        if problems:
            print(f"    rejected {row.get('creator_name', '?')}: {'; '.join(problems)}")
            return tool_result(block.id, "Not saved:\n- " + "\n- ".join(problems)
                               + "\nFix these and call record_creator again, or skip this creator.", error=True)
        row.update(source_pass=source, recorded_at=now_iso())
        state["creators"].append(row)
        qualified = count_qualified(state)
        print(f"    + {row['creator_name']} - {row['book_title']} (fit {row['fit_score']}) [{qualified}/{target}]")
        text = f"Saved {row['creator_name']}. The list has {qualified} creators with fit_score 2 or 3 (goal {target})."
        if qualified >= target:
            text += " The goal is reached: finish this pass now with your pass note."
        return tool_result(block.id, text)
    if block.name == "report_inaccessible_source":
        entry = {k: str(block.input.get(k, "")) for k in ("source", "url", "reason")}
        entry["pass"] = source
        state["inaccessible"].append(entry)
        print(f"    x could not use {entry['source']}: {entry['reason']}")
        return tool_result(block.id, "Noted.")
    return tool_result(block.id, f"Unknown tool {block.name}.", error=True)


def add_usage(state, usage):
    totals = state["usage"]
    for field in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        totals[field] = totals.get(field, 0) + (getattr(usage, field, None) or 0)
    server = getattr(usage, "server_tool_use", None)
    for field in ("web_search_requests", "web_fetch_requests"):
        totals[field] = totals.get(field, 0) + (getattr(server, field, None) or 0)


def call_claude(client, args, system, messages):
    params = dict(
        model=args.model,
        max_tokens=64000,
        system=system,
        messages=messages,
        tools=TOOLS,
        thinking={"type": "adaptive"},
        output_config={"effort": args.effort},
        cache_control={"type": "ephemeral"},
    )
    params.update(
        betas=["server-side-fallback-2026-07-01", "compact-2026-01-12"],
        # On a safety decline, rerun the turn on Anthropic's recommended fallback model.
        fallbacks="default",
        # Fetched pages pile up over a long pass; summarize older turns instead of running out of context.
        context_management={"edits": [{
            "type": "compact_20260112",
            "trigger": {"type": "input_tokens", "value": 200_000},
            "instructions": COMPACTION_INSTRUCTIONS,
        }]},
    )
    with client.beta.messages.stream(**params) as stream:
        return stream.get_final_message()


def run_pass(client, args, state, source, system, out):
    messages = [{"role": "user", "content": pass_prompt(source, state, args.target, date.today())}]
    info = state["passes"].setdefault(source, {})
    info.update(status="in_progress", started_at=now_iso())
    note = f"Stopped after {args.max_turns} turns (--max-turns)."
    for _ in range(args.max_turns):
        response = call_claude(client, args, system, messages)
        add_usage(state, response.usage)
        if response.stop_reason == "refusal":
            note = "Stopped: the model declined to continue this pass."
            break
        messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if block.type == "server_tool_use" and block.name in ("web_search", "web_fetch"):
                print(f"    {block.name}: {block.input.get('query') or block.input.get('url')}")
        if response.stop_reason == "pause_turn":
            continue  # the server resumes its own search loop when we send the turn back
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            note = "\n".join(b.text for b in response.content if b.type == "text").strip()
            break
        if response.stop_reason == "max_tokens":
            results = [tool_result(b.id, "Your reply was cut off before this call was complete. Make it again.",
                                   error=True) for b in tool_uses]
        else:
            results = [handle_tool(b, state, source, args.target) for b in tool_uses]
        messages.append({"role": "user", "content": results})
        save_state(state, out)
    info.update(status="done", note=note, finished_at=now_iso())
    save_state(state, out)
    print(f"  note: {note}")


def cmd_run(args):
    out = Path(args.out)
    state = load_state(out)
    system = SYSTEM.format(brief=BRIEF.read_text().strip())
    sources = args.sources.split(",") if args.sources else list(SOURCES)
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        sys.exit(f"Unknown source(s) {unknown}; choose from {', '.join(SOURCES)}")

    if args.dry_run:
        print("=== system ===\n" + system)
        for source in sources:
            print(f"\n=== pass: {source} ===\n" + pass_prompt(source, state, args.target, date.today()))
        return 0

    import anthropic
    client = anthropic.Anthropic(max_retries=4)
    code = 0
    try:
        for source in sources:
            if count_qualified(state) >= args.target:
                print(f"Goal of {args.target} reached.")
                break
            if state["passes"].get(source, {}).get("status") == "done" and not args.redo:
                print(f"== {source}: already done, skipping (use --redo to run it again)")
                continue
            print(f"== {source}")
            run_pass(client, args, state, source, system, out)
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to resume.")
        code = 130
    except anthropic.APIStatusError as e:
        print(f"\nAPI error {e.status_code}: {e.message}", file=sys.stderr)
        code = 1
    except anthropic.APIConnectionError as e:
        print(f"\nCould not reach the API: {e}", file=sys.stderr)
        code = 1
    finally:
        save_state(state, out)
    u = state["usage"]
    print(f"\nUsage so far: {u.get('input_tokens', 0):,} input tokens "
          f"({u.get('cache_read_input_tokens', 0):,} cached reads), {u.get('output_tokens', 0):,} output tokens, "
          f"{u.get('web_search_requests', 0)} searches, {u.get('web_fetch_requests', 0)} fetches.")
    export(state, out, args.target)
    return code


# -------------------------------------------------------------------- export

def ranked(creators):
    return sorted(creators, key=lambda c: (-c["fit_score"], -c["traction_strength"], c["creator_name"].casefold()))


def write_csv(rows, path):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def price_numbers(price):
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", price.replace(",", ""))]


def summary_markdown(state, rows, target):
    creators = state["creators"]
    qualified = count_qualified(state)
    lines = [f"# Prospect list summary", "",
             f"Generated {date.today().isoformat()}. {len(creators)} creators recorded; "
             f"{len(rows)} rows in prospects.csv, of which {sum(r['fit_score'] >= 2 for r in rows)} score 2 or 3.", ""]
    if qualified < target:
        ones = sum(r["fit_score"] == 1 for r in rows)
        lines += [f"**Short of the goal:** {qualified} creators scored 2 or 3 against a goal of {target}. "
                  + (f"The list is topped up with {ones} borderline (fit 1) creators. " if ones else "")
                  + "See the pass notes for where each source ran dry.", ""]

    lines += ["## By source", "", "| Pass | Recorded | Fit 3 | Fit 2 | Fit 1 | In CSV |", "|---|---|---|---|---|---|"]
    in_csv = {id(r) for r in rows}
    for source in dict.fromkeys([c["source_pass"] for c in creators] + list(state["passes"])):
        mine = [c for c in creators if c.get("source_pass") == source]
        fits = [sum(c["fit_score"] == f for c in mine) for f in (3, 2, 1)]
        lines.append(f"| {source} | {len(mine)} | {fits[0]} | {fits[1]} | {fits[2]} | {sum(id(c) in in_csv for c in mine)} |")

    with_sample = sum(bool(r["sample_url"]) for r in rows)
    course = {v: sum(r["has_course"] == v for r in rows) for v in HAS_COURSE}
    single = [price_numbers(r["price_usd"])[0] for r in rows if len(price_numbers(r["price_usd"])) == 1]
    platforms = {}
    for r in rows:
        platforms[r["platform"]] = platforms.get(r["platform"], 0) + 1
    lines += ["", "## The exported rows", "",
              f"- Public sample: {with_sample} of {len(rows)}",
              f"- Already sell a course on the material: {course['yes']} yes, {course['unclear']} unclear, {course['no']} no",
              "- Platforms: " + (", ".join(f"{k} {v}" for k, v in sorted(platforms.items(), key=lambda kv: -kv[1])) or "none")]
    if single:
        lines.append(f"- Fixed prices (USD, {len(single)} rows): median {statistics.median(single):g}, "
                     f"range {min(single):g} to {max(single):g}")

    lines += ["", "## Pass notes", ""]
    for source, info in state["passes"].items():
        lines += [f"**{source}** ({info.get('status', '?')}): {info.get('note', '')}", ""]

    lines += ["## Sources that couldn't be used", ""]
    lines += [f"- {e['source']} ({e['url']}): {e['reason']}" for e in state["inaccessible"]] or ["- None reported."]
    return "\n".join(lines) + "\n"


def export(state, out, target):
    if not state["creators"]:
        print("No creators recorded yet; nothing to export.")
        return
    rows = ranked(state["creators"])[:target]
    write_csv(rows, out / "prospects.csv")
    (out / "summary.md").write_text(summary_markdown(state, rows, target), encoding="utf-8")
    print(f"Wrote {out / 'prospects.csv'} ({len(rows)} rows) and {out / 'summary.md'}.")


def cmd_export(args):
    out = Path(args.out)
    export(load_state(out), out, args.target)
    return 0


# ---------------------------------------------------------------- validation

def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], list(reader)


def cmd_validate(args):
    path = Path(args.file or Path(args.out) / "prospects.csv")
    header, rows = read_csv(path)
    errors, warnings = [], []
    if header != COLUMNS:
        errors.append(f"columns must be exactly: {','.join(COLUMNS)}")
    if len(rows) != args.target:
        warnings.append(f"{len(rows)} rows, expected {args.target}")
    seen_names, seen_urls = {}, {}
    for i, row in enumerate(rows, 2):  # line 1 is the header
        errors += [f"line {i}: {p}" for p in row_problems(row)]
        for key, seen in ((name_key(row.get("creator_name", "")), seen_names),
                          (url_key(row.get("book_url", "")), seen_urls)):
            if key and key in seen:
                errors.append(f"line {i}: duplicate of line {seen[key]}")
            seen.setdefault(key, i)
    fits = [int(r["fit_score"]) for r in rows if str(r.get("fit_score")) in ("1", "2", "3")]
    if fits != sorted(fits, reverse=True):
        errors.append("rows are not sorted by fit_score descending")
    for w in warnings:
        print(f"warning {w}")
    for e in errors:
        print(f"error   {e}")
    print(f"{path}: {len(rows)} rows, {len(errors)} errors, {len(warnings)} warnings.")
    return 1 if errors else 0


# --------------------------------------------------------------- link check

USER_AGENT = "DripbookLinkCheck/1.0 (one request per URL)"


def cmd_check_links(args):
    path = Path(args.file or Path(args.out) / "prospects.csv")
    _, rows = read_csv(path)
    robots, last_hit, broken = {}, {}, 0
    for row in rows:
        for field in ("book_url", "sample_url", "contact_channel"):
            url = row.get(field, "")
            if not URL.match(url):
                continue
            host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
            if any(host == d or host.endswith("." + d) for d in NO_FETCH_DOMAINS):
                status = "not checked (needs login)"
            else:
                if host not in robots:
                    parts = urllib.parse.urlsplit(url)
                    rp = urllib.robotparser.RobotFileParser(f"{parts.scheme}://{parts.netloc}/robots.txt")
                    try:
                        rp.read()
                    except (urllib.error.URLError, OSError):
                        rp = None  # no readable robots.txt: allowed
                    robots[host] = rp
                if robots[host] is not None and not robots[host].can_fetch(USER_AGENT, url):
                    status = "skipped (robots.txt)"
                else:
                    wait = args.delay - (time.monotonic() - last_hit.get(host, -1e9))
                    if wait > 0:
                        time.sleep(wait)
                    last_hit[host] = time.monotonic()
                    status = fetch_status(url)
                    if status.startswith(("404", "410", "unreachable")):
                        broken += 1
            print(f"{status:28} {row['creator_name'][:28]:28} {field:16} {url}")
    print(f"\n{broken} broken link(s). 401/403 usually means the site blocks scripts; open those by hand.")
    return 1 if broken else 0


def fetch_status(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return f"{resp.status} ok"
    except urllib.error.HTTPError as e:
        return f"{e.code}"
    except (urllib.error.URLError, OSError) as e:
        return f"unreachable ({getattr(e, 'reason', e)})"


# ---------------------------------------------------------------------- main

def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", default=str(DEFAULT_OUT), help="output folder (default: discovery/output)")
    common.add_argument("--target", type=int, default=50, help="number of creators wanted (default: 50)")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="research creators with Claude (resumable)")
    run.add_argument("--sources", help=f"comma-separated passes to run, from: {','.join(SOURCES)}")
    run.add_argument("--model", default="claude-opus-5-5", choices=MODELS)
    run.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    run.add_argument("--max-turns", type=int, default=40, help="API turns per pass before giving up (default: 40)")
    run.add_argument("--redo", action="store_true", help="rerun passes already marked done")
    run.add_argument("--dry-run", action="store_true", help="print the prompts and exit")
    run.set_defaults(func=cmd_run)

    sub.add_parser("export", parents=[common], help="rewrite prospects.csv and summary.md from saved state").set_defaults(func=cmd_export)

    val = sub.add_parser("validate", parents=[common], help="check a CSV against the brief's format")
    val.add_argument("file", nargs="?", help="default: <out>/prospects.csv")
    val.set_defaults(func=cmd_validate)

    links = sub.add_parser("check-links", parents=[common], help="confirm the URLs in a CSV still load")
    links.add_argument("file", nargs="?", help="default: <out>/prospects.csv")
    links.add_argument("--delay", type=float, default=2.0, help="seconds between requests to one host (default: 2)")
    links.set_defaults(func=cmd_check_links)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
