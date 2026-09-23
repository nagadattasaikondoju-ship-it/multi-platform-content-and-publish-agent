"""Vercel entrypoint: exposes the Grow it FastAPI app as `app`."""

from grow_it.web.app import create_app

app = create_app()
