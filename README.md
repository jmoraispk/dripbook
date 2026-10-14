# dripbook
Turn your ebook into a paid email course.

| Path | What it is |
|---|---|
| `site/` | The public landing page. Plain HTML and CSS, no build step. |
| `scripts/site_check.py` | Pre-launch check for `site/`: broken links and unfilled settings. |
| `vercel.json` | Hosting config: serves `site/` on Vercel. |
| `discovery/` | Customer discovery: finds ebook creators to pitch. Not part of the website. |
| `converter/` | Turns an ebook PDF or its free sample into email lessons, for private previews. |

## Website

Preview it locally:

```bash
python3 -m http.server 8000 --directory site
```

Then open http://localhost:8000.

### Before it goes live

The pages contain three settings written as `{{NAME}}`. Fill them in once:

```bash
python3 scripts/site_check.py fill SITE_URL=https://your-domain.com CONTACT_EMAIL=you@your-domain.com FORM_ENDPOINT=https://formspree.io/f/yourid
```

- `SITE_URL` is the public address, used for the link preview image on social sites.
- `CONTACT_EMAIL` is used by the Contact link, the privacy page and the form's fallback.
- `FORM_ENDPOINT` receives preview requests from the sign-up form. Any backend that accepts a form POST and returns JSON works; [Formspree](https://formspree.io) has a free tier. Until it is set, the form opens the visitor's email app addressed to `CONTACT_EMAIL`.

Also check:

- The revenue share shown on the page (`SHARE = 10` in `site/index.html`).
- The privacy page (`site/privacy.html`) matches the form provider you picked.

Then run the check. It exits non-zero if a link is broken or a setting is still empty:

```bash
python3 scripts/site_check.py --strict
```

### Deploying

The repository is private, so use a host whose free plan builds from private repos. With Vercel:

1. Import the GitHub repository at vercel.com/new. `vercel.json` already sets the output folder to `site/` with no build step.
2. Deploy, then add your domain under Project → Settings → Domains.

Netlify and Cloudflare Pages work the same way: no build command, publish directory `site`. GitHub Pages also works, but needs a paid GitHub plan for a private repository.

## Customer discovery

`discovery/discover.py` builds a list of self-published programming ebook creators to pitch, following the rules in `discovery/brief.md`. Claude searches each source in turn (Leanpub, Gumroad, Payhip and Lemon Squeezy, newsletters, X and LinkedIn), checks each creator against the brief on public pages, and saves the ones that qualify. It never contacts anyone.

It needs [uv](https://docs.astral.sh/uv/) and an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run discovery/discover.py run
```

Results go to `discovery/output/`, which git ignores because it holds people's contact details:

- `prospects.csv`: up to 50 creators, sorted by fit score and then traction
- `summary.md`: which sources worked, sample and course counts, pricing, sources that couldn't be used
- `state.json`: everything recorded so far. Stopping with Ctrl-C is safe, and running the same command again picks up where it left off.

Other commands:

```bash
uv run discovery/discover.py run --dry-run                 # show the prompts without calling the API
uv run discovery/discover.py run --sources gumroad --target 20
uv run discovery/discover.py check-links                   # confirm the CSV's URLs still load
uv run discovery/discover.py validate                      # check the CSV against the brief's format
python3 -m unittest discover -s discovery                  # offline tests
```

A full run makes a few hundred web searches and fetches on Claude Opus 5.5. A rough estimate, not yet measured, is tens of dollars. Try one source first with `--sources leanpub --target 10`; the script prints token and search counts at the end, so you can see the quality and cost before running everything.

## Converter

`converter/convert.py` turns a non-fiction ebook PDF, or just its free sample, into a Dripbook course and renders a preview to send privately to the author. Lessons are excerpts of the book, never rewrites: Claude only decides where each lesson starts and ends and writes the subject line, an optional one-line lead-in and a one-line "Tomorrow:" teaser. The script copies the lesson text from the PDF itself and checks that the lessons cover the book in order, so nothing is invented or silently dropped. Code blocks, lists and headings keep their exact text and indentation. Figures can't be carried over yet; each one is replaced by a note pointing to its page and listed in the report.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run converter/convert.py sample.pdf                     # plan with Claude, then render
uv run converter/convert.py https://example.com/book.pdf --pages 23-38   # a URL, and only some pages
```

Results go to `converter/output/<book-slug>/`, which git ignores:

- `lessons.json`: the whole plan, with every lesson's text, read time and pages, plus figures that weren't carried over, skipped pages and warnings
- `day-01.html`, `day-01.txt`, ...: the free lessons (`--free`, default 3) as email-ready HTML and plain text. The last free one ends with the "continue the course" button (`--price`, `--cta-url`). `{{CHECKOUT_URL}}` and `{{UNSUBSCRIBE_URL}}` are placeholders for whatever sends the emails.
- `preview.html`: the lesson sequence and the free emails in one page, in the landing page's style

To render without the API, pass a plan. `lessons.json` from an earlier run works, so you can edit subjects or move split points and render again. `converter/examples/think-python.plan.json` is a hand-made plan for chapters 1 and 2 of [Think Python](https://greenteapress.com/thinkpython2/thinkpython2.pdf) (CC BY-NC):

```bash
uv run converter/convert.py converter/output/thinkpython2.pdf --pages 23-38 --plan converter/examples/think-python.plan.json
uv run converter/convert.py book.pdf --blocks              # list the numbered blocks a plan refers to
uv run converter/convert.py book.pdf --dry-run             # show the prompts without calling the API
python3 -m unittest discover -s converter                  # offline tests
```

Long books are planned one chapter-sized part at a time (`--chunk-words`, default 12,000), so a whole book costs about one pass over its text in input tokens. Not measured yet: try a sample first. Known limits: scanned PDFs (no OCR), two-column layouts, and math formulas, which come out as plain text.
