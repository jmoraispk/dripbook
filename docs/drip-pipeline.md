# Daily drips: sign-up and sending

*2026-11-16.* A reader picks a book and a pace on the home page, confirms by email, gets part 1 at once, then one part every morning.

## Decisions

- **Storage: Upstash Redis** (Vercel Marketplace, free plan). It has a REST API, so the functions need only Python's standard library: no driver, no `requirements.txt`, no schema. The data is key-value anyway. The free plan (256 MB, 500K commands a month) covers the trial with lots of room.
- **Parts are cut when they're sent, not ahead of time.** The first time someone picks a book, it's read from Gutenberg's own mirror, split into blocks once and cached in Redis for 90 days, gzipped (about 300 KB a book). The key carries `READER` (`drip/app.py`): raise it when reading changes, so cached books are read again. Cutting it again for each send takes milliseconds, and it always gives the same parts. Precomputing would mean uploading 958 books × 3 paces to Blob again every time the cutter changes.
- **Parts come from the HTML edition**, so emails keep headings, set-off examples and quotations, verse, italics and the printed page numbers (`drip/gutenberg_html.py`). The plain text is the fallback when the HTML's word count is more than 15% off the text's: 18 of 952 books, mostly ones whose text is in images.
- **Every part opens with where you are:** book and part, chapter and section, printed pages (or % through when the edition has none), minutes, and a progress bar.
- **Python functions, standard library only**, tested with `unittest` like the rest of the repo.

## How it works

| Step | Where |
|---|---|
| The dialog posts `{email, book, pace, tz}` | `site/home.js` → `api/subscribe.py` |
| Confirmation email, transactional stream `outbound`. Unconfirmed sign-ups expire after 7 days | `drip/app.py` `subscribe` |
| The link opens a page that posts itself, so it's one click for the reader, but mail scanners that only follow links can't confirm. Without JavaScript the page shows a button. Part 1 goes out at once | `api/confirm.py` |
| 06:00 UTC: each active reader who hasn't had a part today gets the next one, on stream `broadcast-1` | `api/cron.py`, `vercel.json` |
| "Send me the next part now", the same pattern | `api/next.py` |
| Book requests from How it works: every request is its own entry in the `book-requests` list; only an identical one from the same IP within 10 minutes is dropped. No email is sent | `site/how.js` → `api/request.py` |
| Search also covers the rest of Gutenberg: about 30,000 English books that pass the same public-domain rules (`site/catalog.json`, built by `scripts/build_catalog.py`, loaded on the first search). These get **Request book** instead of a sign-up; the request keeps the Gutenberg number. When neither has a match (or the reader asks), the page also searches scans: the Internet Archive straight from the browser, and HathiTrust from our copy of its public-domain list, static files on Cloudflare R2 that the browser reads by byte range (`scripts/hathi_search.py`; `HATHI_R2` in `site/home.js`). Those requests keep `ia:`/`hathi:` ids | `site/home.js` → `api/request.py` |
| Your own PDF: the editor (`/cuts?for=me`) posts each lesson's text as blocks. It's stored as `own:{id}` and sent like any book: same confirmation, mornings and header. Kept until 30 days after the drip ends; at most 200 at once, 900 KB compressed each | `site/cuts.html` → `api/own.py` |
| Its pictures: each figure is cut from the page in the browser (JPEG or PNG, up to 1200 px wide) and sent one lesson at a time to a private Vercel Blob store, then the confirmation email goes. Each part's email carries its pictures as inline attachments (`cid:`), placed in the text. Blob deletes them by itself: 8 days if unconfirmed, then the drip's length plus 60 days, then 31 days once it ends. At most 25 MB a book, 7 MB an email. Without `BLOB_READ_WRITE_TOKEN`, drips go out with captions only | `site/cuts.html` → `api/pictures.py` |
| Unsubscribe: Postmark's link (`{{{ pm:unsubscribe }}}`). When Postmark refuses a send (406), the subscription stops | `drip/app.py` `send_next` |

The cutter (`drip/cut.py`) works on blocks with Gutenberg's header, footer, credits, contents, illustrations and transcriber's notes and a closing index already removed, page markers turned into page numbers, and each footnote moved to just after the paragraph that cites it. Part 1 opens on chapter one when the title page, contents and preface before it are under 5% of the book (514 of 952 books). Longer openings are kept, because they're usually the author's own prologue, as in The Turn of the Screw; only the title page and contents before them are skipped (a play keeps its list of characters). `KEEP_FRONT_MATTER` lists books whose short opening is worth reading too (Darwin's introduction, Gulliver's letters). The rest is cut at paragraph breaks into parts of 230 words × minutes, with a new part at a chapter heading once the current part is half full. A part ends only where a sentence does, so a paragraph that runs on into a quotation or poem stays whole, and a line that leads in ('…is as follows:') keeps the heading or paragraph it introduces; a paragraph too long for one part is split at full stops, and at semicolons only inside a sentence longer than a part. Past 1.5× the target a part ends anyway (unpunctuated verse, chess notation). Check a book with `python3 drip/cut.py public-domain/books/1342/pg1342.txt --pace 10`. A sweep of all 958 books on 2026-11-22 found and fixed: title pages in part 1 (220 → 155 books, the rest mostly false alarms), footnotes days away from their marker (6,522 → 1,770 parts), `[pg 12]` shown as text (7,033 → 2), parts ending mid-sentence at 10 minutes (1,896 → 1,297), transcriber's notes and credits (426 → 4 blocks), and 3 books with no text: two left out of the library (`NOT_DRIPS` in `scripts/build_library.py`), and Haeckel's Evolution of Man read from its HTML. A second pass on 2026-11-23 cut parts that end mid-text at 10 minutes from 124 to 21 (Ulysses' last chapter, indexes with no heading); the other 847 parts ending without a stop end where a section does (journal entries, signatures, ornaments).

## Setup (once)

1. **Vercel → dripbook → Storage → Create Database → Upstash for Redis**, free plan, connected to all environments. This adds `KV_REST_API_URL` and `KV_REST_API_TOKEN`.
2. **Settings → Environment Variables:**
   - `POSTMARK_SERVER_TOKEN`: Postmark → Servers → Dripbook → API Tokens. Mark it Sensitive.
   - `CRON_SECRET`: any long random string (`openssl rand -hex 32`). Vercel sends it with each cron call.
   - `BLOB_READ_WRITE_TOKEN`: added by **Storage → Create Database → Blob**, with access set to **Private**. Without it, own-PDF drips have no pictures.
   - `ALLOWED_EMAILS`: until launch, the only addresses that can sign up, e.g. `you@x.com, @dripbook.ai`. Remove it to open sign-up.
3. Redeploy. Until steps 1 and 2 are done, the form answers "Sign-up isn't set up on this server yet."

Cron jobs only run on the production deployment. To run the morning job by hand:

```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://dripbook.ai/api/cron
```

## Who signed up

`python3 scripts/export_signups.py` writes `exports/subscribers.csv` (every `sub:*` record: email, book, status, parts sent, dates) and `exports/requests.csv`, and prints the most-requested titles. It reads `KV_REST_API_URL` and `KV_REST_API_TOKEN` from the shell or from `.env.local` (`vercel env pull .env.local`). Both files are git-ignored. To browse instead, use the Data Browser for this database in the Upstash console.

Books outside Gutenberg: `python3 scripts/sources.py serve` (add `--host 0.0.0.0` to reach it from another machine) is a local search page over the Internet Archive and HathiTrust; it also serves the HathiTrust search files by byte range, so the site's search box on a private address searches them like R2. It cleans an Archive scan's OCR text into parts and can start a drip of it through `/api/own`, like a reader's own PDF. See the script's docstring.

Tests: `python3 -m unittest discover -s drip`

## Not done yet

- One send time for everyone (06:00 UTC). Each reader's time zone is saved at sign-up for when they can pick a time. That needs an hourly cron, which needs Vercel Pro.
- Postmark's unsubscribe stops every book for that address.
- Changing the cutter renumbers parts for readers already partway through a book.
- No limit on sign-ups per IP beyond the one-minute resend guard.

HathiTrust on the live site: one R2 bucket with a public custom domain (or r2.dev while testing) and a CORS rule allowing GET with a `Range` header from dripbook.ai. Put `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_BUCKET` in `.env.local`, run `python3 scripts/hathi_search.py build`, then `uv run --with boto3 scripts/hathi_search.py upload`, and `HATHI_R2` points at the bucket (`https://assets.dripbook.ai/hathi/`, bucket `dripbook-assets`). Rebuild and upload when the list is refreshed; each upload is a new version folder.
