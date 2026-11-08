# Email spike

This sends one lesson through Postmark, to check the account, the `mail.dripbook.ai` domain and the Broadcast stream. Account setup is in [docs/email-delivery.md](../docs/email-delivery.md). It uses Python's standard library only.

1. **Check the message format without an account.** Postmark validates it with its test token and sends nothing:

   ```bash
   python3 email-spike/send.py --to you@dripbook.ai --dry-run
   ```

2. **Send for real** once the domain is verified. Until Postmark approves the account, send only to an @dripbook.ai address:

   ```bash
   POSTMARK_SERVER_TOKEN=... python3 email-spike/send.py --to you@dripbook.ai
   ```

3. **Send a lesson from the converter:**

   ```bash
   POSTMARK_SERVER_TOKEN=... python3 email-spike/send.py --to you@dripbook.ai --html path/to/lesson-1.html --subject "Day 1: ..."
   ```

In Gmail, open the message, then **⋮ → Show original**. SPF, DKIM and DMARC should all say PASS, and the footer should have Postmark's unsubscribe link.

Offline tests:

```bash
python3 -m unittest discover -s email-spike
```
