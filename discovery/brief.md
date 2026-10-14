# Brief: build a prospect list of ebook creators for Dripbook

`discover.py` sends this whole file to Claude as the research brief. Edit it to change who qualifies. Two parts are also fixed in the script, so change them in both places: the CSV columns in "What to record", and the research passes in "Where to look" (`SOURCES` in `discover.py`).

## Context

Dripbook is an early-stage product that turns a non-fiction ebook (PDF) into a paid email course. The creator uploads their book, Dripbook splits it into short daily lessons, and the creator sells the course from a Dripbook page. Dripbook takes a share of each sale. Readers get a free preview (the first few lessons), then pay to unlock the rest.

We are about to do manual outreach to a small number of creators. For each one, we will turn their free sample chapter into a short email-course preview and send it to them privately. Your job is only to build the list. Do not contact anyone.

## Goal

Produce a list of **50 qualified creators** who self-publish **programming or software-engineering ebooks** and would be good candidates for this pitch.

## Who qualifies

Include a creator only if all of these are true:

1. They self-publish a non-fiction ebook on a programming or software topic, such as a language (Python, Rust, Go, TypeScript), system design, DevOps, data engineering, ML engineering, or interview prep.
2. They sell it themselves, not through a traditional publisher such as O'Reilly, Manning, or Pragmatic Bookshelf.
3. The book is available as a PDF or something easily converted to one.
4. There are public signs that it sells or has an audience: reviews, ratings, a sales count, an active newsletter, or a meaningful social following.

Prefer creators who:

- Offer a free sample chapter, preview, or table of contents that is publicly accessible. This matters most, because we need a sample to build the preview.
- Do **not** already sell a video or email course on the same material.
- Are active now, with a post, update or new release in the last 12 months.
- Have more than one book or product, which suggests repeat sellers.

Exclude:

- Books from traditional publishers.
- Fiction, general self-help, or non-technical business books (out of scope for this first round).
- Creators whose only public contact is a personal phone number or home address.
- Anything requiring a login or purchase to evaluate.

## Where to look

Start with these, roughly in order of expected yield:

1. **Leanpub** (leanpub.com): browse the computer-programming categories and bestseller lists. Most books have free samples.
2. **Gumroad**: Gumroad Discover and web searches like `site:gumroad.com rust ebook`, `site:gumroad.com "system design" pdf`.
3. **Payhip and Lemon Squeezy**: web searches like `site:payhip.com python ebook` or `"lemonsqueezy.com" ebook golang`.
4. **Newsletter writers who also sell an ebook**: search Substack and Beehiiv for engineering newsletters that link to a paid ebook.
5. **X and LinkedIn**: developers promoting their own ebook. Use web search to find them; only use what's visible without logging in.

## What to record

One row per creator with these columns, in this order:

| Column | What goes in it |
|---|---|
| `creator_name` | Person or brand name |
| `book_title` | Title of the main ebook |
| `book_url` | Link to the sales page |
| `platform` | Leanpub, Gumroad, Payhip, Lemon Squeezy, own site, other |
| `topic` | Short topic, e.g. "Rust for Python developers" |
| `price_usd` | Listed price, or a range if it varies; blank if not shown |
| `sample_url` | Link to the free sample, preview or table of contents; blank if none |
| `traction` | Public evidence of sales or audience, quoted or summarized, e.g. "4.6 stars, 210 ratings" or "newsletter ~12k subscribers (per their site)" |
| `has_course` | yes / no / unclear: do they already sell a course on this material? |
| `last_active` | Most recent date you can see activity (release, update, post) |
| `contact_channel` | A public, business-appropriate way to reach them: contact form URL, business email shown on their site, or public social profile URL |
| `fit_score` | 1 to 3 (see below) |
| `notes` | Anything useful for a personal pitch, e.g. "updated book in March for Rust 1.80" |

### Fit score

- **3:** meets all requirements, has a public sample, shows clear traction, no existing course, active in the last 12 months.
- **2:** meets requirements but is missing one preferred signal (no sample, weak traction, or not recently active).
- **1:** borderline; include only if you can't reach 50 with 2s and 3s.

Sort the final list by `fit_score` descending, then by strength of traction.

## Rules

- **Do not contact anyone**, sign up for newsletters, buy anything or create accounts.
- Use only information that is publicly visible without logging in. Don't scrape private data or bypass paywalls.
- Respect each site's terms and robots rules. Search and browse at a normal pace; no bulk scraping.
- Record only business-appropriate contact channels. No personal phone numbers or home addresses.
- Don't guess. If a field isn't verifiable, leave it blank or write "unclear". Don't invent traction numbers.
- One row per creator. If someone has several books, pick the one that best fits and mention the others in `notes`.

## Done means

1. A CSV file with 50 rows, all columns present, sorted as described.
2. A short summary (a few sentences) covering: which sources produced the most qualified creators, how many have public samples, how many already sell courses, and anything surprising you noticed about pricing or the market.
3. A list of any sources you couldn't access and why.

If you find fewer than 50 qualified creators after working through all the sources, stop and report how many you found and where the pool ran dry, rather than lowering the bar.
