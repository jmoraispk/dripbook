#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["stripe>=11"]
# ///
"""Test-mode spike for Dripbook payments: a reader buys a public-domain drip from Dripbook.

Dripbook is the seller, so this is plain Stripe Checkout on Dripbook's own account,
with no creator payouts. A webhook marks the reader as paid, which is where the
real app unlocks the remaining lessons. Stripe sandbox only.

  uv run payments-spike/spike.py serve                 # webhook + return pages on localhost:3000
  uv run payments-spike/spike.py checkout              # print a Checkout link for a $20 sale
  uv run payments-spike/spike.py checkout --price 9 --reader r_42
  uv run payments-spike/spike.py status                # the last sale as Stripe sees it
  uv run payments-spike/spike.py refund                # refund the last sale

Needs STRIPE_SECRET_KEY set to a test key (sk_test_...). `serve` also needs
STRIPE_WEBHOOK_SECRET, which `stripe listen` prints. The last sale is kept in
payments-spike/state.json, which git ignores.
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


def stripe_fee(price_cents):
    """Stripe's standard US card fee on one sale, 2.9% + 30 cents. International cards cost 1.5% more."""
    return round(price_cents * 0.029) + 30


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


def last_sale():
    sale = load_state().get("last_sale")
    if not sale:
        sys.exit("No sale yet. Run: uv run payments-spike/spike.py checkout")
    return sale


def cmd_checkout(args):
    stripe = stripe_client()
    price = round(args.price * 100)
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": price,
                "product_data": {"name": f"{args.book}: the remaining lessons"},
            },
            "quantity": 1,
        }],
        client_reference_id=args.reader,
        metadata={"reader_id": args.reader},
        payment_intent_data={"metadata": {"reader_id": args.reader}},
        success_url=f"{BASE}/paid?session={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE}/cancelled",
    )
    save_state(last_sale={"session": session.id, "reader": args.reader, "price": price})
    keep = price - stripe_fee(price)
    print(f"Sale of ${price / 100:.2f}. Dripbook keeps about ${keep / 100:.2f} after Stripe's fee.")
    print("Pay with card 4242 4242 4242 4242, any future date, any CVC:")
    print(session.url)


def cmd_status(args):
    stripe = stripe_client()
    session = stripe.checkout.Session.retrieve(last_sale()["session"])
    print(f"{session.id}: status={session.status} payment_status={session.payment_status} "
          f"amount=${(session.amount_total or 0) / 100:.2f} reader={session.client_reference_id}")


def cmd_refund(args):
    stripe = stripe_client()
    sale = last_sale()
    if not sale.get("payment_intent"):
        sys.exit("The last sale isn't paid yet. The webhook records it when checkout completes (run `serve`).")
    refund = stripe.Refund.create(payment_intent=sale["payment_intent"])
    print(f"Refund {refund.id}: {refund.status}. Stripe keeps its card fee on refunds.")


def handle_event(event):
    """React to one verified webhook event, as parsed JSON. Returns a line for the log."""
    kind = event["type"]
    obj = event["data"]["object"]
    if kind == "checkout.session.completed":
        reader = obj.get("client_reference_id")
        save_state(last_sale=(load_state().get("last_sale") or {}) | {"payment_intent": obj.get("payment_intent")})
        return f"PAID: reader {reader}, ${(obj.get('amount_total') or 0) / 100:.2f}. The real app unlocks the remaining lessons here."
    return f"ignored {kind}"


PAGES = {
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
    c = sub.add_parser("checkout", help="print a Checkout link for one sale")
    c.add_argument("--price", type=float, default=20.0, help="price in dollars (default 20)")
    c.add_argument("--book", default="The Art of War", help="book title shown at checkout")
    c.add_argument("--reader", default="r_test_1", help="fake reader ID passed through to the webhook")
    sub.add_parser("serve", help="run the webhook and return pages on localhost:3000")
    sub.add_parser("status", help="show the last sale as Stripe sees it")
    sub.add_parser("refund", help="refund the last paid sale")
    args = p.parse_args(argv)
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
