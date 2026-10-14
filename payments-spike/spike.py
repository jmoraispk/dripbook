#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["stripe>=11"]
# ///
"""Test-mode spike for Dripbook payments: one creator, one sale, Dripbook's fee.

It proves the Stripe Connect setup chosen in docs/email-delivery.md, in a Stripe
sandbox only. The creator gets a connected account with an Express dashboard and
pays Stripe's card fee (direct charges). Dripbook takes max(10%, $2) as an
application fee. A webhook marks the reader as paid.

  uv run payments-spike/spike.py account               # create the creator's connected account
  uv run payments-spike/spike.py onboard               # print Stripe's onboarding link for it
  uv run payments-spike/spike.py serve                 # webhook + return pages on localhost:3000
  uv run payments-spike/spike.py checkout              # print a Checkout link for a $20 sale
  uv run payments-spike/spike.py checkout --price 49 --reader r_42
  uv run payments-spike/spike.py status                # account state and fees Dripbook collected
  uv run payments-spike/spike.py refund                # refund the last sale, Dripbook keeps its fee
  uv run payments-spike/spike.py refund --with-fee     # refund the last sale and Dripbook's fee

Needs STRIPE_SECRET_KEY set to a test key (sk_test_...). `serve` also needs
STRIPE_WEBHOOK_SECRET, which `stripe listen` prints. The account ID and the last
sale are kept in payments-spike/state.json, which git ignores.
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
PORT = 3000
BASE = f"http://localhost:{PORT}"

FEE_SHARE = 0.10
FEE_MIN_CENTS = 200

# The configuration under test. Stripe's docs don't list it as unsupported, but
# don't say outright that it's allowed: `account` is where we find out.
CONTROLLER = {
    "fees": {"payer": "account"},  # the creator pays Stripe's card fee
    "losses": {"payments": "stripe"},  # Stripe, not Dripbook, covers negative balances
    "requirement_collection": "stripe",  # Stripe's hosted onboarding collects ID and bank details
    "stripe_dashboard": {"type": "express"},  # the creator gets the light dashboard
}


def application_fee(price_cents):
    """Dripbook's fee on one sale: 10% of the price, at least $2, never more than the price."""
    return min(price_cents, max(FEE_MIN_CENTS, round(price_cents * FEE_SHARE)))


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(**changes):
    state = load_state() | changes
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    return state


def stripe_client():
    import stripe

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key.startswith(("sk_test_", "rk_test_")):
        sys.exit("Set STRIPE_SECRET_KEY to a test key (sk_test_...). This spike never uses live keys.")
    stripe.api_key = key
    return stripe


def account_id():
    acct = load_state().get("account")
    if not acct:
        sys.exit("No connected account yet. Run: uv run payments-spike/spike.py account")
    return acct


def cmd_account(args):
    stripe = stripe_client()
    params = {
        "controller": CONTROLLER,
        "capabilities": {"card_payments": {"requested": True}, "transfers": {"requested": True}},
    }
    if args.country:
        params["country"] = args.country
    if args.email:
        params["email"] = args.email
    try:
        acct = stripe.Account.create(**params)
    except stripe.StripeError as e:
        print("Stripe rejected the account configuration:", file=sys.stderr)
        print(f"  {e.user_message or e}", file=sys.stderr)
        print("  controller =", json.dumps(CONTROLLER), file=sys.stderr)
        sys.exit(1)
    save_state(account=acct.id, last_sale=None)
    print(f"Created {acct.id}")
    print("  controller:", acct.controller)
    print("Next: uv run payments-spike/spike.py onboard")


def cmd_onboard(args):
    stripe = stripe_client()
    link = stripe.AccountLink.create(
        account=account_id(),
        refresh_url=f"{BASE}/onboard/refresh",
        return_url=f"{BASE}/onboard/return",
        type="account_onboarding",
    )
    print("Open this link and use Stripe's test-data buttons to finish onboarding:")
    print(link.url)
    print("The link works once and expires in a few minutes. Run `onboard` again for a new one.")


def cmd_checkout(args):
    stripe = stripe_client()
    acct = account_id()
    price = round(args.price * 100)
    fee = application_fee(price)
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": price,
                "product_data": {"name": "Practical Rust: the remaining lessons"},
            },
            "quantity": 1,
        }],
        payment_intent_data={"application_fee_amount": fee, "metadata": {"reader_id": args.reader}},
        client_reference_id=args.reader,
        metadata={"reader_id": args.reader},
        success_url=f"{BASE}/paid?session={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE}/cancelled",
        stripe_account=acct,  # direct charge: the sale happens on the creator's account
    )
    save_state(last_sale={"session": session.id, "reader": args.reader, "price": price, "fee": fee})
    print(f"Sale of ${price / 100:.2f} on {acct}, Dripbook fee ${fee / 100:.2f}.")
    print("Pay with card 4242 4242 4242 4242, any future date, any CVC:")
    print(session.url)


def cmd_status(args):
    stripe = stripe_client()
    acct = stripe.Account.retrieve(account_id())
    print(f"{acct.id}: charges_enabled={acct.charges_enabled} payouts_enabled={acct.payouts_enabled}")
    due = getattr(acct.requirements, "currently_due", None) or []
    if due:
        print("  still needed for onboarding:", ", ".join(due))
    print("Fees Dripbook collected (latest 5):")
    fees = stripe.ApplicationFee.list(limit=5)
    for f in fees.data:
        refunded = f" (refunded ${f.amount_refunded / 100:.2f})" if f.amount_refunded else ""
        print(f"  {f.id}: ${f.amount / 100:.2f} from {f.account}{refunded}")
    if not fees.data:
        print("  none yet")


def cmd_refund(args):
    stripe = stripe_client()
    sale = load_state().get("last_sale") or {}
    if not sale.get("payment_intent"):
        sys.exit("No paid sale recorded yet. The webhook records it when checkout completes (run `serve`).")
    refund = stripe.Refund.create(
        payment_intent=sale["payment_intent"],
        refund_application_fee=args.with_fee,
        stripe_account=account_id(),
    )
    kept = "returned to the creator" if args.with_fee else "kept by Dripbook"
    print(f"Refund {refund.id}: {refund.status}. Dripbook's ${sale['fee'] / 100:.2f} fee was {kept}.")
    print("Check with: uv run payments-spike/spike.py status")


def handle_event(event):
    """React to one verified webhook event, as parsed JSON. Returns a line for the log."""
    kind = event["type"]
    obj = event["data"]["object"]
    acct = event.get("account")
    if kind == "checkout.session.completed":
        reader = obj.get("client_reference_id")
        save_state(last_sale=(load_state().get("last_sale") or {}) | {"payment_intent": obj.get("payment_intent")})
        return f"PAID: reader {reader} on {acct}. The real app unlocks the remaining lessons here."
    if kind == "account.updated":
        return f"ACCOUNT {obj.get('id')}: charges_enabled={obj.get('charges_enabled')} payouts_enabled={obj.get('payouts_enabled')}"
    return f"ignored {kind} from {acct or 'platform'}"


PAGES = {
    "/onboard/return": "Onboarding finished or paused. Run `spike.py status` to see whether charges are enabled.",
    "/onboard/refresh": "That onboarding link expired. Run `spike.py onboard` for a new one.",
    "/paid": "Payment received. Watch the `serve` terminal for the webhook.",
    "/cancelled": "Checkout cancelled.",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = PAGES.get(self.path.split("?")[0])
        self._reply(200 if page else 404, page or "Not found")

    def do_POST(self):
        if self.path != "/api/stripe-webhook":
            return self._reply(404, "Not found")
        import stripe

        payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            stripe.Webhook.construct_event(
                payload, self.headers.get("Stripe-Signature", ""), os.environ["STRIPE_WEBHOOK_SECRET"]
            )
        except (ValueError, stripe.SignatureVerificationError):
            print("Rejected a webhook with a bad signature", flush=True)
            return self._reply(400, "Bad signature")
        print(handle_event(json.loads(payload)), flush=True)
        self._reply(200, "ok")

    def _reply(self, status, text):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def cmd_serve(args):
    if not os.environ.get("STRIPE_WEBHOOK_SECRET", "").startswith("whsec_"):
        sys.exit("Set STRIPE_WEBHOOK_SECRET to the whsec_... value that `stripe listen` prints.")
    print(f"Listening on {BASE}. Webhooks go to {BASE}/api/stripe-webhook", flush=True)
    HTTPServer(("localhost", PORT), Handler).serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("account", help="create the creator's connected account")
    a.add_argument("--country", help="two-letter country code; defaults to the platform's country")
    a.add_argument("--email", help="the creator's email, prefilled in onboarding")
    sub.add_parser("onboard", help="print Stripe's onboarding link")
    c = sub.add_parser("checkout", help="print a Checkout link for one sale")
    c.add_argument("--price", type=float, default=20.0, help="price in dollars (default 20)")
    c.add_argument("--reader", default="r_test_1", help="fake reader ID passed through to the webhook")
    sub.add_parser("serve", help="run the webhook and return pages on localhost:3000")
    sub.add_parser("status", help="show account state and collected fees")
    r = sub.add_parser("refund", help="refund the last paid sale")
    r.add_argument("--with-fee", action="store_true", help="also return Dripbook's fee")
    args = p.parse_args(argv)
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
