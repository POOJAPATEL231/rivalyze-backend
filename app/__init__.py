"""Rivalyze backend package.

Loads .env (if python-dotenv is installed) at import time so every submodule
sees a consistent environment — MOCK_MODE, provider keys, BEARER_TOKEN, etc.
The load is optional: offline MOCK runs work with no .env and no dotenv.

Then, if AZURE_KEY_VAULT_URL is set, secrets are pulled from Azure Key Vault
into the environment — BEFORE core.config reads it — so deployed runs source
their secrets from the vault instead of a .env file. This is a no-op offline.
"""
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is a convenience, never a hard dependency
    pass

# Must run after load_dotenv() (so .env still overrides) and before any
# `from .core import config` (so config sees the vault-sourced values).
from .core.keyvault import load_into_env  # noqa: E402

load_into_env()
