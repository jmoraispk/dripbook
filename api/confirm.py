"""Vercel function: /api/confirm. The logic is in drip/web.py and drip/app.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "drip"))
import web  # noqa: E402

# Vercel only deploys a .py file as a function if it declares `class handler`.
class handler(web.handler_for(get=web.confirm_get, post=web.confirm_post)):
    pass
