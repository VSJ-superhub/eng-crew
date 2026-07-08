"""eng-crew dashboard — FastAPI web UI for monitoring runs and approving plans."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
    from eng_crew.config import Settings


def create_app(settings: "Settings | None" = None) -> "FastAPI":
    """Return the canonical eng-crew dashboard app.

    The full dashboard (SPA + system/intake/projects/runs routers, including the
    HITL approve flow) is defined as a module-level app in
    ``eng_crew.dashboard.app``. This factory exists so callers such as the CLI
    (`eng-crew dashboard`) get exactly the same app as
    ``python -m eng_crew.dashboard`` instead of a divergent stub.

    ``settings`` is accepted for backward compatibility; the app reads its
    configuration from the environment via ``eng_crew.config`` at import time.
    """
    from eng_crew.dashboard.app import app
    return app
