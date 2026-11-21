"""Vercel function: /api/pictures, the pictures of a reader's own PDF. The logic is in drip/web.py and drip/app.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "drip"))
import web  # noqa: E402

# Vercel only deploys a .py file as a function if it declares `class handler`.
class handler(web.handler_for(post=web.pictures, kind=web.JSON, limit=4_500_000)):
    pass
