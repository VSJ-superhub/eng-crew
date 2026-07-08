import os
from pathlib import Path

# Load .env so ANTHROPIC_API_KEY etc. are available to the app (intake chat,
# providers) which read os.environ directly.
_env = Path(__file__).parent.parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

from eng_crew.config import load_settings

import uvicorn
from .app import app

# Single source of truth for host/port: the config (ENG_CREW_DASHBOARD_PORT,
# default 9000) — the same value the CLI `eng-crew dashboard` command uses.
_cfg = load_settings()
_host = _cfg.dashboard_host
_port = _cfg.dashboard_port
print(f"Dashboard: http://{_host}:{_port}")
uvicorn.run(app, host=_host, port=_port)