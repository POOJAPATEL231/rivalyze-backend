"""Azure Key Vault loader — offline unit tests (the Azure SDK is faked).

Covers: no-op when unset (offline path), env-name -> secret-name mapping,
vault load with env-precedence, missing-secret skip (not fatal), and strict
mode raising on failure. No network and no real vault required.

Run:  MOCK_MODE=1 python -m pytest tests/contract/test_keyvault.py -q
"""
import os

import pytest

from app.core import keyvault


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in keyvault.SECRET_KEYS + ("AZURE_KEY_VAULT_URL", "AZURE_KEY_VAULT_STRICT"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_noop_when_url_unset():
    keyvault.load_into_env()  # must not touch env, must not import azure
    assert all(os.getenv(k) is None for k in keyvault.SECRET_KEYS)


def test_kv_secret_name_mapping():
    assert keyvault.kv_secret_name("GEMINI_API_KEY") == "GEMINI-API-KEY"
    assert keyvault.kv_secret_name("DATABASE_URL") == "DATABASE-URL"


class _FakeSecret:
    def __init__(self, value):
        self.value = value


def _install_fake_vault(monkeypatch, store, init_error=None, fetch_error_for=()):
    import azure.identity
    import azure.keyvault.secrets
    from azure.core.exceptions import ResourceNotFoundError

    class FakeCredential:
        def __init__(self, *a, **k):
            pass

    class FakeClient:
        def __init__(self, vault_url, credential):
            if init_error:
                raise init_error

        def get_secret(self, name):
            if name in fetch_error_for:
                raise RuntimeError("transient vault error")
            if name not in store:
                raise ResourceNotFoundError(f"{name} not found")
            return _FakeSecret(store[name])

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(azure.keyvault.secrets, "SecretClient", FakeClient)


def test_loads_from_vault_and_env_wins(monkeypatch):
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://vault.example/")
    monkeypatch.setenv("GROQ_API_KEY", "from-env")  # pre-set -> must NOT be overwritten
    _install_fake_vault(monkeypatch, {
        "GROQ-API-KEY": "from-vault",
        "GEMINI-API-KEY": "gem-secret",
        "JWT-SECRET": "jwt-secret",
    })

    keyvault.load_into_env()

    assert os.getenv("GROQ_API_KEY") == "from-env"       # env precedence preserved
    assert os.getenv("GEMINI_API_KEY") == "gem-secret"   # filled from the vault
    assert os.getenv("JWT_SECRET") == "jwt-secret"
    assert os.getenv("TAVILY_API_KEY") is None           # absent in vault -> skipped


def test_missing_secret_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://vault.example/")
    _install_fake_vault(monkeypatch, {"GEMINI-API-KEY": "x"})  # only one present
    keyvault.load_into_env()  # must not raise
    assert os.getenv("GEMINI_API_KEY") == "x"


def test_fetch_error_non_strict_is_swallowed(monkeypatch):
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://vault.example/")
    _install_fake_vault(monkeypatch, {"GEMINI-API-KEY": "x"},
                        fetch_error_for=("JWT-SECRET",))
    keyvault.load_into_env()  # JWT fetch errors -> logged + skipped, others still load
    assert os.getenv("GEMINI_API_KEY") == "x"
    assert os.getenv("JWT_SECRET") is None


def test_strict_raises_on_client_error(monkeypatch):
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://vault.example/")
    monkeypatch.setenv("AZURE_KEY_VAULT_STRICT", "1")
    _install_fake_vault(monkeypatch, {}, init_error=RuntimeError("credential failure"))
    with pytest.raises(RuntimeError):
        keyvault.load_into_env()
