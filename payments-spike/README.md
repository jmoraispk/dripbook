# Payments spike

This checks a Stripe sale end to end in a sandbox. A reader buys a drip from Dripbook, and the webhook marks them as paid. No real money is involved. Account setup is in [docs/email-delivery.md](../docs/email-delivery.md).

Use three terminals in the repo root. Terminal 1 forwards Stripe events to your machine and prints a `whsec_...` secret:

```bash
stripe listen --events checkout.session.completed --forward-to localhost:3000/api/stripe-webhook
```

Stripe CLI 1.55 and later require `--events`. Add `--api-key sk_test_...` to skip `stripe login`.

Terminal 2 runs the server, using that secret:

```bash
STRIPE_SECRET_KEY=sk_test_... STRIPE_WEBHOOK_SECRET=whsec_... uv run payments-spike/spike.py serve
```

Terminal 3, with `STRIPE_SECRET_KEY` exported:

1. `uv run payments-spike/spike.py checkout` prints a link for a $20 sale. Pay with `4242 4242 4242 4242`, any future date, any CVC. Terminal 2 should show `PAID: reader r_test_1, $20.00`.
2. `uv run payments-spike/spike.py status` should show `payment_status=paid`. The sale also appears under **Payments** in the dashboard.
3. `uv run payments-spike/spike.py refund` refunds it. Stripe keeps its card fee.

Offline tests:

```bash
uv run --with stripe python -m unittest discover -s payments-spike
```
