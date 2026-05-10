import os
from pathlib import Path

# Load .env so ANTHROPIC_API_KEY is available for intake chat
_env = Path(__file__).parent.parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

import uvicorn
from .app import app

port = int(os.environ.get("DASHBOARD_PORT", 8090))
print(f"Dashboard: http://localhost:{port}")
uvicorn.run(app, host="127.0.0.1", port=port)