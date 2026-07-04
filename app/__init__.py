"""Rivalyze backend package.

Loads .env (if python-dotenv is installed) at import time so every submodule
sees a consistent environment — MOCK_MODE, provider keys, BEARER_TOKEN, etc.
The load is optional: offline MOCK runs work with no .env and no dotenv.
"""
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is a convenience, never a hard dependency
    pass
