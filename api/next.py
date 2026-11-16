"""Vercel function: /api/next. The logic is in drip/web.py and drip/app.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "drip"))
import web  # noqa: E402

handler = web.handler_for(get=web.next_get, post=web.next_post)
