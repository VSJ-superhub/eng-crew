# Load .env so ANTHROPIC_API_KEY etc. are available to the app (intake chat,
# providers) which read os.environ directly. Where that .env lives depends on
# whether this is a checkout or an installed package — config decides.
from eng_crew.config import load_env_into_environ, load_settings

load_env_into_environ()

import uvicorn
from .app import app

# Single source of truth for host/port: the config (ENG_CREW_DASHBOARD_PORT,
# default 9000) — the same value the CLI `eng-crew dashboard` command uses.
_cfg = load_settings()
_host = _cfg.dashboard_host
_port = _cfg.dashboard_port
print(f"Dashboard: http://{_host}:{_port}")
uvicorn.run(app, host=_host, port=_port)