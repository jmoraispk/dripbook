# Email and payments: decisions

*Decided 2026-10-14, with prices checked the same day. Domain: **dripbook.ai**.*

## What the creator does

They send us the PDF, then click one Stripe link to say where their money goes. They don't need an email provider, a domain or an existing Stripe account.

## Email: Postmark on our domain

- Lessons come from `Practical Rust <lessons@mail.dripbook.ai>`, with `Reply-To:` set to the creator's address, so readers' replies go straight to them. The creator sets nothing up.
- One-time DNS on dripbook.ai: Postmark's DKIM record and Return-Path CNAME on `mail.dripbook.ai`, plus `_dmarc.dripbook.ai` set to `v=DMARC1; p=none; rua=mailto:…`. Move it to `quarantine` once reports are clean.
- Lessons go out on a Broadcast stream, and confirmations and receipts on the Transactional stream. Every lesson gets one-click unsubscribe (`List-Unsubscribe` headers). Readers confirm their sign-up by email (double opt-in).
- **Images** live in Vercel Blob (a public store), with only their URLs in Postgres and in the email HTML. Postgres can store image files, but email needs public image URLs anyway, and Gmail strips embedded base64 images.
  - Keep each lesson's HTML under ~100 KB, because Gmail cuts off longer emails.
  - Always set alt text, because some email apps block images by default.
  - Cost: a few cents a month at our scale. Even 1,000 sales a month is roughly 7 GB of image traffic, about $0.35.
- Cold outreach to creators never goes through Postmark. It comes from your personal mailbox.

## Payments: Stripe Connect, direct charges, creator pays card fees

- When a creator is ready to sell, we create a connected account for them and send them Stripe's hosted onboarding link. They fill in their identity and bank details in about 5 minutes, and get a lightweight Express dashboard.
  - Settings: `fees.payer=account`, `losses.payments=stripe`, `requirement_collection=stripe`, `stripe_dashboard.type=express`.
  - Stripe's docs don't list this combination as unsupported. Confirm it in Stripe's Connect setup wizard before building.
- Each Checkout Session runs on the creator's account with `application_fee_amount = max(10% of price, $2)`. Stripe takes its card fee from the creator. Our fee arrives clean, with no per-account or payout fees.
- The identity form can't be skipped. Anyone receiving payouts must be verified. Collecting all the money ourselves and paying creators by hand would make Dripbook the seller, responsible for sales tax and VAT everywhere. It would also break Stripe's rules on collecting money for others, which Connect exists to handle.

## Costs

| Item | Cost |
|---|---|
| Postmark Basic | $15/mo for 10k emails, then $1.80 per 1k. Pro is cheaper above ~13k emails. |
| Vercel Pro | $20/mo, needed from the first paid sale (Hobby is non-commercial only) |
| Neon Postgres, Vercel Blob | ~$0 at this scale |
| **Fixed total** | **~$36/mo, plus the domain** |

Each sale uses about 67 emails, assuming 10% of free readers buy, which is about $0.12. At $20 we keep $1.88 per sale, so ~20 sales a month cover fixed costs. At $49 we keep $4.78, so ~8 do.

Converting a book is a one-time cost per book, not per sale. AI processing that costs $5 a book is paid back by the first 3 sales at $20.

## Build order

1. Open a Postmark account and add `mail.dripbook.ai` with the DNS above. Apply with the site live, because Postmark reviews new accounts by hand.
2. Store the converter's lessons in Postgres and their images in Blob.
3. Build the course page: sign-up, confirmation email, lesson 1 sent right away.
4. Add a daily Vercel Cron sender, plus pause, "send the next one now" and unsubscribe.
   - Pause and "send the next one now" open a page with a button. Corporate link scanners open every link, so these must never act on the click itself.
5. Add Stripe Connect onboarding, then Checkout. A `checkout.session.completed` webhook unlocks the remaining lessons.
6. Pilot with one creator, and move to Vercel Pro just before the first paid sale.

Sources (checked 2026-10-14): [Postmark pricing](https://postmarkapp.com/pricing), [Stripe pricing](https://stripe.com/pricing), [Connect pricing](https://stripe.com/connect/pricing), [Connect controller properties](https://docs.stripe.com/connect/migrate-to-controller-properties), [Vercel Blob pricing](https://vercel.com/docs/vercel-blob/usage-and-pricing), [Vercel Hobby](https://vercel.com/docs/plans/hobby), [Gmail sender rules](https://support.google.com/a/answer/81126).
