"""Offline tests for spike.py: uv run --with stripe python -m unittest discover -s payments-spike

No Stripe account or network is needed. Webhooks are signed locally with a fake
secret, the same way Stripe signs them. Tests that need the stripe package skip
without it.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import spike  # noqa: E402

try:
    import stripe  # noqa: F401
except ImportError:
    stripe = None

SECRET = "whsec_test_secret"


def sign(payload, secret=SECRET):
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


class TempState(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(spike, "STATE", Path(tmp.name) / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)


class FeeTest(unittest.TestCase):
    def test_ten_percent_above_twenty_dollars(self):
        self.assertEqual(spike.application_fee(4900), 490)
        self.assertEqual(spike.application_fee(9900), 990)

    def test_two_dollar_minimum(self):
        self.assertEqual(spike.application_fee(2000), 200)
        self.assertEqual(spike.application_fee(900), 200)

    def test_never_more_than_the_price(self):
        self.assertEqual(spike.application_fee(150), 150)


class HandleEventTest(TempState):
    def test_paid_checkout_records_the_payment_intent(self):
        spike.save_state(account="acct_1", last_sale={"session": "cs_1", "fee": 200})
        line = spike.handle_event({
            "type": "checkout.session.completed",
            "account": "acct_1",
            "data": {"object": {"client_reference_id": "r_7", "payment_intent": "pi_9"}},
        })
        self.assertIn("reader r_7 on acct_1", line)
        self.assertEqual(spike.load_state()["last_sale"], {"session": "cs_1", "fee": 200, "payment_intent": "pi_9"})

    def test_account_updated_reports_charges(self):
        line = spike.handle_event({
            "type": "account.updated",
            "account": "acct_1",
            "data": {"object": {"id": "acct_1", "charges_enabled": True, "payouts_enabled": False}},
        })
        self.assertIn("charges_enabled=True", line)


class KeyGuardTest(unittest.TestCase):
    @unittest.skipUnless(stripe, "stripe not installed")
    def test_refuses_live_keys(self):
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_live_abc"}):
            with self.assertRaises(SystemExit):
                spike.stripe_client()


@unittest.skipUnless(stripe, "stripe not installed")
class WebhookServerTest(TempState):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET})
        env.start()
        self.addCleanup(env.stop)
        self.server = HTTPServer(("localhost", 0), spike.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://localhost:{self.server.server_port}/api/stripe-webhook"

    def post(self, payload, signature):
        req = urllib.request.Request(self.url, data=payload.encode(), method="POST",
                                     headers={"Stripe-Signature": signature, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_signed_event_is_accepted(self):
        payload = json.dumps({
            "id": "evt_1", "object": "event", "type": "checkout.session.completed", "account": "acct_1",
            "data": {"object": {"client_reference_id": "r_1", "payment_intent": "pi_1"}},
        })
        with mock.patch("builtins.print"):
            self.assertEqual(self.post(payload, sign(payload)), 200)
        self.assertEqual(spike.load_state()["last_sale"]["payment_intent"], "pi_1")

    def test_bad_signature_is_rejected(self):
        payload = json.dumps({"id": "evt_2", "object": "event", "type": "account.updated", "data": {"object": {}}})
        with mock.patch("builtins.print"):
            self.assertEqual(self.post(payload, sign(payload, "whsec_wrong")), 400)
        self.assertEqual(spike.load_state(), {})


if __name__ == "__main__":
    unittest.main()
