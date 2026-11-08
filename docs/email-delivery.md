# Email and payments: setup

*Updated 2026-11-08. Domain: **dripbook.ai**.*

**Plan for now:** Dripbook sells drips of public-domain books itself. There are no creator payouts, so we use a plain Stripe account and no Stripe Connect. Connect comes back when creators join. The 2026-10-14 version of this doc covers it, in git history.

## Postmark (email)

1. Sign up at postmarkapp.com and create a server called "Dripbook". Under **Message Streams**, make sure there's a Broadcast stream with the ID `broadcast`. Lessons go out on it, and Postmark adds the unsubscribe link and headers. Receipts and sign-in links use the default transactional stream, `outbound`.
2. Go to **Sender Signatures → Add Domain** and enter `mail.dripbook.ai`. Add the DKIM TXT record and the Return-Path CNAME it shows at your DNS provider, then click **Verify**.
3. Add a TXT record at `_dmarc.dripbook.ai` with the value `v=DMARC1; p=none; rua=mailto:dmarc@dripbook.ai`. Change `p=none` to `p=quarantine` once a few weeks of reports are clean.
4. **Request approval.** Say that readers sign up for daily lessons of public-domain books, confirm by email, and can unsubscribe in one click, with no bought lists. Until Postmark approves the account, it only delivers to addresses at dripbook.ai, so you need a mailbox (or forwarding) there.
5. Copy the token under **API Tokens** and send a test (instructions in [email-spike/](../email-spike/README.md)). In Gmail, open **Show original** and check that SPF, DKIM and DMARC all say PASS.

Images go in Vercel Blob, and only their URLs go in the HTML. Keep each lesson under ~100 KB, because Gmail cuts off longer emails.

## Stripe (payments)

1. Sign up at stripe.com and switch to a sandbox. Under **Settings → Business**, set the public name "Dripbook", a support email and the statement descriptor `DRIPBOOK`, which is what appears on card statements. Add a logo under **Settings → Branding**.
2. Copy the sandbox secret key (`sk_test_...`) from **Developers → API keys**. Install the Stripe CLI and run `stripe login`.
3. Run a test sale (instructions in [payments-spike/](../payments-spike/README.md)). The last free lesson links to Checkout. When a `checkout.session.completed` webhook arrives, the reader is marked paid and the remaining lessons unlock.
4. **Before live:** activate the account (business and bank details), add a live webhook endpoint at `https://dripbook.ai/api/stripe-webhook`, and move Vercel to Pro, because Hobby is non-commercial only.

Each $20 sale costs 2.9% + 30¢ in card fees, so Dripbook keeps $19.12. Fixed costs: Postmark $15/mo and Vercel Pro $20/mo.

## Decide before selling

- **Sales tax and VAT.** Dripbook is now the seller, so it owes tax wherever the law requires it. Digital sales to EU buyers owe VAT from the first sale. Stripe Tax can calculate and collect it (0.5% per sale). Check with an accountant where you have to register.
- **Is it really public domain?** Pick books that are public domain in both the US and the EU: published before 1930, and the author died over 70 years ago. Translations have their own copyright. If the text comes from Project Gutenberg, remove its header, footer and name before selling.
- **Think Python can't be sold.** The demo book is licensed CC BY-NC (non-commercial).
