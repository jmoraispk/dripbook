# Upload flow: design

*Drafted 2026-10-14. Vercel limits checked the same day; sources at the end. Builds on [email-delivery.md](email-delivery.md).*

## Where we are

- The site's Upload PDF button opens a Dropbox file request, which also asks for a name and email.
- The founder downloads the PDF, runs `uv run converter/convert.py book.pdf`, checks `preview.html`, and sends it privately.
- This works and should stay the default until a few creators have been through it. The site promises "We assemble every book by hand", so nothing below should make a course go live without a person looking at it first.

Two problems exist today, before any automation:

1. **Previews don't travel well.** `preview.html` links to its figures as `figures/…` next to it, so emailing the HTML file alone loses every image. An unlisted link works better than an attachment.
2. **The privacy page doesn't mention Claude.** It says uploads go through Dropbox, but the converter sends the book's text to Anthropic's API to plan the lessons. Add that, and check what Anthropic's commercial terms say about retention and training before writing the sentence.

## Phases

### Phase 1: publish previews as links (small; do now)

- Add `--publish` to `convert.py`. After rendering, it uploads `preview.html`, the emails and `figures/` to a **public** Blob store under a random path, such as `previews/k7f3…/`, and prints the link. It also renders with `--image-base-url` pointing at that path, so the emails' images work once sent.
- Use the official Python SDK (`vercel` on PyPI) with `BLOB_READ_WRITE_TOKEN`, so it works from a laptop.
- The link is unlisted, not private. Anyone who has it can open it. That's fine for a free sample, but don't publish whole paid books this way.
- Add `--unpublish <link>` to delete a preview when a creator says no.

### Phase 2: conversion as a background job (with step 2 of email-delivery.md)

The founder still triggers it, but the work runs on Vercel and the result lands in Postgres and Blob, which the sender needs anyway.

- PDFs go in a **private** Blob store. The founder uploads with a CLI command, or a job copies them over from Dropbox.
- A Python worker runs each conversion step (below) and writes progress to Postgres.
- The result is the same `lessons.json` the converter writes today, stored as `jsonb`. Emails are rendered from it at send time, so fixing a template never means converting a book again.
- Figures go to the public Blob store at `books/<book_id>/figures/…`, and their URLs are stored, as email-delivery.md already decided.

### Phase 3: self-serve upload page (replaces Dropbox)

- `dripbook.ai/upload`: a static page in the house style. It has a file input (PDF only), email, book title, and a checkbox: "I own the rights to this book or have permission to sell it".
- The PDF goes **directly from the browser to the private Blob store** (a client upload), so it never passes through a function. Functions can't receive request bodies over 4.5 MB, and books are often bigger.
- Before handing out an upload token, the token route checks a bot test (Vercel BotID or Turnstile), the content type (`application/pdf`) and a size cap (say 100 MB). It also rate-limits by IP.
- **Nothing expensive runs until the email is verified.** When the upload completes, we create the book row and send a "Confirm your email" link. Clicking it starts the conversion. That ties every Claude call to a real inbox, and stops anonymous uploads from running up the bill.
- The worker checks the file really is a PDF (`%PDF` header), counts pages (refusing over ~800), and rejects scanned books with a clear message, since there's no OCR.
- When conversion finishes, the book waits for a person (`ready_for_review`). The founder opens the preview, fixes anything, and releases it. The creator then gets an email with their preview link, plus **Approve** and **Request changes** buttons that lead to a page with a confirm button. Corporate link scanners open every link, so the email links must never act on the click itself, as email-delivery.md already notes for pause and resume.

### Phase 4: creator edits

- In a web editor, the creator renames subject lines, merges or splits lessons, and chooses how many are free.
- A split edit is just a new `first_block` / `last_block`, and `plan_errors()` already validates those. Lesson text still comes only from the PDF.

## How the conversion runs on Vercel

**Constraints** (Fluid compute, checked 2026-10-14):

- **Duration:** a function may run 300 s on Hobby, and 800 s on Pro by default (1,800 s in beta).
- **Memory:** 2 GB by default.
- **Bundle size:** Python bundles can be 500 MB uncompressed.
- **Python:** 3.12 to 3.14, with dependencies from `pyproject.toml` or `requirements.txt`.
- **Plan:** commercial use needs Pro anyway (email-delivery.md).

**Will the converter run there?** Probably, but nothing has been measured on Vercel yet.

- **Dependencies:** pdfplumber (pure Python, plus pypdfium2 and Pillow, which ship as prebuilt binary packages for Linux) and `anthropic` total tens of MB, far under 500 MB.
- **Reading the PDF:** about 13 s for all 244 pages of Think Python on a laptop.
- **Planning:** the slow part is the Claude calls, one per chapter-sized part (8 parts for Think Python). At `high` effort with adaptive thinking, one call could take a minute or more.

**So split the work into short steps, one queue message each:**

| Step | Input | Output |
|---|---|---|
| `extract` | PDF in private Blob | blocks JSON (private Blob), part boundaries, page count, warnings |
| `plan_part` ×N | blocks of one part, the previous part's last lesson, the next part's opening | that part's lessons, stored per part |
| `assemble` | all parts | `build_course()`; plan errors or `lessons.json` in Postgres |
| `figures` | PDF, figure boxes | PNGs in public Blob, URLs in `lessons.json` |
| `preview` | `lessons.json` | preview page and first emails; status `ready_for_review` |

- **Queue:** use Vercel Queues with the Python SDK (`vercel` / `vercel-queue`). Subscribers are declared in `pyproject.toml`, and the SDK keeps renewing the lease while a handler runs. Delivery is at-least-once with retries, so every step must be safe to repeat: key results by `(book_id, step, part)`, and skip a step whose result already exists.
- **Parts in order:** `plan_part` runs one part after another, not in parallel. Each part's prompt names the previous lesson, and the last lesson's teaser looks ahead. Each part finishing enqueues the next.
- **Fallback:** Queues is in beta. If it misbehaves, a `jobs` table in Postgres plus a scheduled function that runs the next pending step does the same job with GA pieces. Check the cron frequency your plan allows.
- **Layout:** the site stays static (`outputDirectory: site`). Add Python functions under `api/`, which Vercel still supports alongside a static site. The client-upload token route is a few lines with `handleUpload` from `@vercel/blob`, so it can be one small JS file in `api/`. I only found client-upload helpers documented for the JS SDK. If the project grows a frontend framework later, move to Vercel Services (beta), which put a frontend and a Python backend in one project.

## What `convert.py` needs to become a library

Most of it already is. Reading, planning and rendering are separate functions that take plain dicts. The changes:

1. **Settings object instead of `args`.** `call_claude`, `plan_chunk` and `plan_with_claude` read `args.model`, `args.effort` and `args.chunk_words`. Pass a small settings object instead, which the CLI builds from argparse.
2. **One part at a time.** Split `plan_with_claude()` into `plan_part(doc, part_index, previous_lessons) -> part_plan` and `merge_parts(parts) -> raw_plan`. The CLI loop calls the same two.
3. **Storage hooks.** `render_figures()` already takes `crop_image`. Give it a `save(name, image) -> url` hook too, so the worker writes to Blob instead of a folder, and drop the local `figures/` assumption from `write_outputs()`.
4. **Entry points.** Add `convert_pdf(path, settings) -> (course, errors)` for the whole pipeline, which the CLI and the Phase 2 manual trigger can share.
5. **Packaging.** Keep `convert.py` a single file with its PEP 723 header for local use. The worker imports it and pins the same dependencies in `requirements.txt`. The offline tests keep covering the library functions, since the worker adds only glue.

## Data

Postgres, as in email-delivery.md:

- `creators`: `id`, `email`, `email_verified_at`, `created_at`
- `books`: `id`, `creator_id`, `title`, `pdf_path` (private Blob), `sha256`, `pages`, `status` (`uploaded` → `verified` → `converting` → `ready_for_review` → `sent_to_creator` → `approved`, or `failed`), `rights_confirmed_at`, `error`, `claude_input_tokens`, `claude_output_tokens`, timestamps
- `conversion_steps`: `book_id`, `step`, `part`, `status`, `attempts`, `result` (`jsonb`), `error`, timestamps. Its unique key `(book_id, step, part)` is what makes retries safe.
- `courses`: `book_id`, `plan` (`lessons.json` as `jsonb`), `free_lessons`, `price`, `approved_at`

**Retention:**

- **PDFs:** delete them a set time after the course is approved or the creator walks away, say 30 days. The plan keeps the lesson text, so the PDF isn't needed to send emails, only to convert again.
- **Previews:** delete them when a creator declines.
- **The privacy page** must state both rules.

## Costs (estimates, not measured)

- **Claude, per book:** a 250-page book is about 110k input tokens, plus about 10k thinking and output tokens per part. At Opus 5.5's $4 / $20 per million tokens, that's roughly $1 to $3, more if a part needs a retry. Measure on the first ten books and store token counts per book (the `books` columns above).
- **Vercel functions:** while the worker waits on Claude, active CPU isn't billed, only provisioned memory. Small at this volume.
- **Blob storage:** PDFs stay a few months at most, and figures are tens of KB each, so pennies.

## Open questions

1. **Anthropic's data terms.** What exactly to tell creators about their book text going through Anthropic's API (retention, training, zero-retention options), and whether any creator will refuse. Settle this before Phase 3.
2. **Whole books vs. samples.** For outreach, previews come from free samples. Self-serve uploads will be whole paid books, so private storage, deletion and the rights checkbox matter from day one.
3. **Who pays for failed or abandoned conversions.** Do we cap free conversions per verified email, say one book until a first sale?
4. **Founder review gate.** Keep it for every book, or only until quality is proven on N books? Keep it at least through the pilot.
5. **Accounts.** Magic links (email-only sign-in) are enough for preview and approval. A creator dashboard for sales would need real accounts; decide when building Stripe onboarding.
6. **Formats beyond PDF.** EPUB is common on Leanpub and Gumroad and would parse more reliably than PDF. That's out of scope for now, but worth a look once the PDF path works.

## Recommended build order

1. **Phase 1:** add `--publish` and `--unpublish` to the converter, and fix the privacy page to mention Claude. Keep Dropbox.
2. **Library refactor** (the five points above), with tests, still run from the CLI.
3. **Phase 2:** the Postgres tables, the private and public Blob stores, and the queue worker. The founder triggers conversion from a CLI command against a Blob path. Do this alongside step 2 of email-delivery.md's build order.
4. **Phase 3:** the upload page, email verification, the review gate, and the creator's preview page with Approve and Request changes. Remove the Dropbox link and update the privacy page.
5. **Phase 4:** the editor, after the pilot shows what creators actually want to change.

Sources (checked 2026-10-14): [Functions limits](https://vercel.com/docs/functions/limitations), [Python runtime](https://vercel.com/docs/functions/runtimes/python), [Blob client uploads](https://vercel.com/docs/vercel-blob/client-upload), [Blob private storage](https://vercel.com/docs/vercel-blob/private-storage), [Blob pricing and limits](https://vercel.com/docs/vercel-blob/usage-and-pricing), [Queues](https://vercel.com/docs/queues), [Queues Python SDK](https://vercel.com/docs/queues/python-sdk), [Queues limits](https://vercel.com/docs/queues/pricing), [Services](https://vercel.com/docs/services).
