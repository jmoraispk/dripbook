# dripbook
Turn your ebook into a paid email course. Where it's heading: [ROADMAP.md](ROADMAP.md).

| Path | What it is |
|---|---|
| `site/` | The public landing page, plus `cuts.html`, the cut editor. Plain HTML and CSS, no build step. |
| `scripts/site_check.py` | Pre-launch check for `site/`: broken links and unfilled settings. |
| `vercel.json` | Hosting config: serves `site/` and `api/` on Vercel, plus the daily cron. |
| `api/`, `drip/` | Sign-up and the daily email: cuts a book into parts and sends one each morning. See [docs/drip-pipeline.md](docs/drip-pipeline.md). |
| `discovery/` | Customer discovery: finds ebook creators to pitch. Not part of the website. |
| `converter/` | Turns an ebook PDF into daily lessons. |
| `payments-spike/` | Test sale through Stripe Checkout, in a sandbox. Setup in `docs/email-delivery.md`. |
| `email-spike/` | Test send of one lesson through Postmark. |
| `public-domain/` | 1,000 Gutenberg books that are public domain in the US and the EU, so Dripbook can sell them. |

## Website

Preview it locally:

```bash
python3 -m http.server 8000 --directory site
```

Then open http://localhost:8000.

### Before it goes live

The pages contain three settings written as `{{NAME}}`. Fill them in once:

```bash
python3 scripts/site_check.py fill SITE_URL=https://your-domain.com CONTACT_EMAIL=you@your-domain.com EXAMPLE_URL=https://your-domain.com/example/
```

- `SITE_URL` is the public address, used for the link preview image on social sites.
- `CONTACT_EMAIL` is used by the Contact link and the privacy page.
- `EXAMPLE_URL` is the example course preview linked from the upload section, for example a `preview.html` made by the converter.

Books are uploaded through a Dropbox file request, which also asks for the uploader's name and email. Its link is in the `#start` section of `site/index.html`.

Also check:

- The revenue share shown on the page (`SHARE = 10` in `site/index.html`).
- The privacy page (`site/privacy.html`) still matches how uploads are collected.

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

`discovery/discover.py` builds a list of self-published programming ebook creators to pitch, following the rules in `discovery/brief.md`. Claude searches Leanpub (the only source the brief allows), checks each creator against the brief on public pages, and saves the ones that qualify. It never contacts anyone.

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

`converter/convert.py` turns a non-fiction ebook PDF, or just its free sample, into daily lessons: one HTML page per day that reads well in a browser and can be sent as an email, plus a plain-text version. Everything runs on your computer, and the book's text is never sent anywhere.

- Lessons start at the book's headings and run 3 to 5 minutes (`--target-words`, default 900). Short sections are joined. Long sections are cut between paragraphs, never inside a list or between code and the sentence introducing it. Each new chapter starts a new lesson.
- Each lesson's subject is its heading, and its "Tomorrow:" line names the next lesson. Lesson text is copied from the PDF unchanged. Code keeps its exact indentation.
- Figures (drawn diagrams and embedded images) are cropped out as PNGs with the book's caption underneath. A caption with no figure found next to it is reported.
- The table of contents and the index are skipped. Use `--pages` to skip other front matter, such as title pages.

There are two ways to read the PDF, so they can be compared on the same book:

```bash
uv run converter/extract_pymupdf.py book.pdf               # PyMuPDF: about 2.5x faster
uv run converter/extract_pdfplumber.py book.pdf            # pdfplumber
uv run converter/extract_pymupdf.py book.pdf --pages 23-60 # only some pages
```

On all of Think Python (244 pages), both readers make the same 68 lessons and 27 figures, and their text differs in 12 places out of about 3,150 blocks, mostly where a paragraph breaks. PyMuPDF takes 4.4 s instead of 11.3 s and decodes a few characters pdfplumber can't. PyMuPDF is AGPL licensed: fine for running it ourselves, but check the licence before building it into a product.

Results go to `converter/output/<book-slug>/` with pdfplumber, or `<book-slug>-pymupdf/` with PyMuPDF. Git ignores both.

- `preview.html`: the whole sequence. The free lessons (`--free`, default 3) are shown inline, and every other lesson links to its page.
- `day-01.html`, `day-01.txt`, ...: every lesson, as an HTML page and as plain text. The last free one ends with a "continue the course" button (`--price`, `--cta-url`). `{{CHECKOUT_URL}}` and `{{UNSUBSCRIBE_URL}}` are placeholders for whatever sends the emails.
- `figures/`: one PNG per figure. Pages link to them as `figures/…`. If the pages are hosted somewhere else, pass `--image-base-url https://…/` so the links point there.
- `lessons.json`: the plan, with every lesson's text, read time and pages, plus skipped pages and warnings.

To change the split, edit `lessons.json` (subjects, or where lessons start and end) and render it again with `--plan`. `converter/examples/think-python.plan.json` is a hand-made plan for chapters 1 and 2 of [Think Python](https://greenteapress.com/thinkpython2/thinkpython2.pdf) (CC BY-NC):

```bash
uv run converter/convert.py converter/output/thinkpython2.pdf --pages 23-38 --plan converter/examples/think-python.plan.json
uv run converter/convert.py book.pdf --blocks              # list the numbered blocks a plan refers to
python3 -m unittest discover -s converter                  # offline tests
```

`--claude` lets Claude choose the lesson boundaries and write subject lines instead. It sends the book's text to Anthropic's API, so it is never used unless you ask for it.

Known limits: scanned PDFs (no OCR), two-column layouts, and math formulas, which come out as plain text.

### Cut editor

The cut editor is a page for cutting a book into lessons, at [dripbook.ai/cuts](https://dripbook.ai/cuts) (`site/cuts.html`). It also works opened straight from disk. The site's Upload PDF button leads authors there: they drop in their PDF, adjust the cuts, save `cuts.json`, and send both through the Dropbox upload page. Nothing is uploaded: the PDF is read in the browser.

- **A PDF on its own** (what an author would do): the page reads the text and starts with one lesson per chapter, using the PDF's bookmarks or else its largest headings. The front matter, contents and index are skipped. **Split long lessons** cuts every chapter over 6½ minutes at its sections. **Save cuts.json** downloads where each cut is, as a page and a height on it plus the first words after it. Render it with `uv run converter/extract_pymupdf.py book.pdf --cuts cuts.json`. Opening the PDF again with that `cuts.json` (or **Open saved cuts**) picks up where you left off.
- **A PDF with its `lessons.json`** from the converter edits that plan instead, with the converter's own paragraphs and figures. **Save lessons.json** goes back through `--plan` (add the same `--pages` if you used it).
- **Whole book:** every page as a thumbnail, with a coloured band per lesson, a line at each cut and the lesson list beside it. Skipped pages are hatched.
- **One lesson:** its pages large, with its two cuts as black lines. Drag a line to move that cut; it snaps to the gaps between paragraphs. Point at a page and click **Cut here** to split the lesson. You can also merge with the next or previous lesson, skip pages, split at sections, and edit the subject, intro and "Tomorrow" lines while seeing what readers get.
- Undo and redo, read times and warnings update as you go. Unsaved edits survive a reload.

The cut logic is in `site/cuts-core.js`, with tests in `converter/test_editor_core.mjs`, which run as part of `python3 -m unittest discover -s converter` when Node.js is available (uv can fetch it). The page loads PDF.js from jsDelivr, so it needs an internet connection.
