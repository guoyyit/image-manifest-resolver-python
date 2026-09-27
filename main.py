"""Service entrypoint: ``uvicorn main:app`` or ``uv run fastapi dev``."""

from imr.api import create_app

app = create_app()
