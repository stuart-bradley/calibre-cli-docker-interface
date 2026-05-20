"""Module-level Jinja2Templates instance.

Lives outside main.py to avoid the circular-import dance routes used to do via
`from app.main import templates` at request scope.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
