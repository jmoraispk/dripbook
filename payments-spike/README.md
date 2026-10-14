# Payments spike

This tests in a Stripe sandbox whether one creator can get paid for one sale, with Dripbook keeping max(10%, $2). No real money is involved.

It checks three things:
- **Will Stripe accept the account setup?** The creator pays the card fee and gets the light Express dashboard. Step 2 answers this.
- **Is the fee collected?** Dripbook's fee should arrive clean, with no card fee taken out.
- **Does the webhook fire?** It has to come from the creator's account, because that's what unlocks paid lessons.

## Once

1. Create the Dripbook Stripe account. Open **Connect** and finish the platform setup:
   - creators are paid directly (direct charges);
   - connected accounts pay Stripe's fees;
   - onboarding is hosted by Stripe;
   - creators get the Express dashboard.
2. Copy the sandbox **secret key** (`sk_test_...`).
3. Install the [Stripe CLI](https://docs.stripe.com/stripe-cli) and run `stripe login`.

## Run it

Use three terminals in the repo root. Terminal 1 forwards events from creators' accounts to the local server and prints a `whsec_...` secret:

```bash
stripe listen --forward-connect-to localhost:3000/api/stripe-webhook
```

Terminal 2 runs the server, using that secret:

```bash
STRIPE_SECRET_KEY=sk_test_... STRIPE_WEBHOOK_SECRET=whsec_... uv run payments-spike/spike.py serve
```

Terminal 3, with `STRIPE_SECRET_KEY` exported:

1. `uv run payments-spike/spike.py account` creates a fake creator. **If Stripe rejects the setup, it prints the error here.** That is the main question.
2. `uv run payments-spike/spike.py onboard` prints a link. Open it and use the test-data buttons. Terminal 2 should show `charges_enabled=True`.
3. `uv run payments-spike/spike.py checkout` prints a link for a $20 sale. Pay with `4242 4242 4242 4242`, any future date, any CVC. Terminal 2 should show `PAID: reader r_test_1 on acct_...`.
4. `uv run payments-spike/spike.py status` should list a $2.00 fee. Run `checkout --price 49` to see $4.90.
5. In the Stripe dashboard, **Connect → Collected fees** shows Dripbook's fee. Open the connected account to see the creator's side: the sale, minus Stripe's fee, minus ours.
6. `uv run payments-spike/spike.py refund` refunds the reader and Dripbook keeps its fee. Add `--with-fee` to give our fee back too. Check with `status`. We still need to decide a refund policy.

Offline tests, with no Stripe account needed:

```bash
uv run --with stripe python -m unittest discover -s payments-spike
```

This folder is throwaway. The real app moves this code into Vercel functions.
