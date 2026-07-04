"""Runtime configuration — the one place that reads the environment.

Read once at import (after app/__init__.py has loaded .env). Downstream
modules import the resolved values from here instead of scattering os.getenv
calls. Provider API keys stay OUT of this module on purpose: the llm_router
and search_chain read their own keys lazily so a missing lane is skipped, not
a startup crash.
"""
import os


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) == "1"


# --- run mode ---
MOCK_MODE: bool = _flag("MOCK_MODE")        # 1 = deterministic offline lanes, zero keys
DEMO_RESERVE: bool = _flag("DEMO_RESERVE")  # 1 = hold budget back for the live demo

# --- API surface ---
BEARER_TOKEN: str = os.getenv("BEARER_TOKEN", "")            # empty = auth open (dev)
FRONTEND_ORIGIN: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")

# --- persistence (wired in when Dharvi's repository lands) ---
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
REDIS_URL: str = os.getenv("REDIS_URL", "")

# --- secret source ---
# When set, app/__init__.py has already hydrated os.environ from this Key Vault
# (before this module was imported), so the values above may originate there.
AZURE_KEY_VAULT_URL: str = os.getenv("AZURE_KEY_VAULT_URL", "")
