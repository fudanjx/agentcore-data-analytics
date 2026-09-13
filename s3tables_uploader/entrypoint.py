"""ASGI entrypoint for the lightweight Fargate API service."""

from .api import create_app
from .config import Settings

app = create_app(Settings.from_environ())
