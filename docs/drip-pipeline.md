# Daily drips: sign-up and sending

*2026-11-16.* A reader picks a book and a pace on the home page, confirms by email, gets part 1 at once, then one part every morning.

## Decisions

- **Storage: Upstash Redis** (Vercel Marketplace, free plan). It has a REST API, so the functions need only Python's standard library: no driver, no `requirements.txt`, no schema. The data is key-value anyway. The free plan (256 MB, 500K commands a month) covers the trial with lots of room.
- **Parts are cut when they're sent, not ahead of time.** The first time someone picks a book, its text comes from Gutenberg's own mirror (the same file as in `public-domain/books/`), is cleaned once and cached in Redis, gzipped (about 300 KB a book). Cutting it again for each send takes milliseconds, and it always gives the same parts. Precomputing would mean uploading 958 books × 3 paces to Blob again every time the cutter changes.
- **Python functions, standard library only**, tested with `unittest` like the rest of the repo.

## How it works

| Step | Where |
|---|---|
| The dialog posts `{email, book, pace}` | `site/home.js` → `api/subscribe.py` |
| Confirmation email, transactional stream `outbound`. Unconfirmed sign-ups expire after 7 days | `drip/app.py` `subscribe` |
| The link opens a page with a button, so mail scanners that follow links can't confirm. Part 1 goes out on the click | `api/confirm.py` |
| 06:00 UTC: each active reader who hasn't had a part today gets the next one, on stream `broadcast-1` | `api/cron.py`, `vercel.json` |
| "Send me the next part now", the same button pattern | `api/next.py` |
| Unsubscribe: Postmark's link (`{{{ pm:unsubscribe }}}`). When Postmark refuses a send (406), the subscription stops | `drip/app.py` `send_next` |

The cutter (`drip/cut.py`) removes Gutenberg's header, footer, credits and illustration tags, then cuts at paragraph breaks into parts of 230 words × minutes. It starts a new part at a chapter heading once the current part is half full. Check a book with `python3 drip/cut.py public-domain/books/1342/pg1342.txt --pace 10`.

## Setup (once)

1. **Vercel → dripbook → Storage → Create Database → Upstash for Redis**, free plan, connected to all environments. This adds `KV_REST_API_URL` and `KV_REST_API_TOKEN`.
2. **Settings → Environment Variables:**
   - `POSTMARK_SERVER_TOKEN`: Postmark → Servers → Dripbook → API Tokens. Mark it Sensitive.
   - `CRON_SECRET`: any long random string (`openssl rand -hex 32`). Vercel sends it with each cron call.
   - `ALLOWED_EMAILS`: until launch, the only addresses that can sign up, e.g. `you@x.com, @dripbook.ai`. Remove it to open sign-up.
3. Redeploy. Until steps 1 and 2 are done, the form answers "Sign-up isn't set up on this server yet."

Cron jobs only run on the production deployment. To run the morning job by hand:

```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://dripbook.ai/api/cron
```

Tests: `python3 -m unittest discover -s drip`

## Not done yet

- Part 1 can be front matter (a preface, a list of illustrations) before chapter 1.
- One send time for everyone (06:00 UTC), and Postmark's unsubscribe stops every book for that address.
- No limit on sign-ups per IP beyond the one-minute resend guard.
