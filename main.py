"""ASGI entry point: ``uv run uvicorn main:app``."""

from image_resolver.service import app

__all__ = ["app"]
