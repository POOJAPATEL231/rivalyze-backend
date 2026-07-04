"""Optional Azure Key Vault secret loader.

Secrets belong in Key Vault, NOT in .env, for any shared/deployed run. When
AZURE_KEY_VAULT_URL is set, this pulls the known secrets from the vault into the
process environment at startup — BEFORE config.py reads them — so every existing
os.getenv / config.py call site keeps working unchanged.

Authentication uses DefaultAzureCredential:
  - In Azure (App Service / Container Apps): the resource's Managed Identity —
    no secret is needed to reach the secret store.
  - Locally: `az login`, or the AZURE_CLIENT_ID / AZURE_CLIENT_SECRET /
    AZURE_TENANT_ID service-principal env vars.

When AZURE_KEY_VAULT_URL is unset (local / MOCK / offline dev) this is a no-op
and the azure-* packages are never imported, so nothing extra is required to run
offline or in the test suite.

Precedence: an existing environment variable ALWAYS wins over the vault — a
local .env value or an explicit override is respected, and the vault only fills
what the environment does not already set. So prod sets ONLY AZURE_KEY_VAULT_URL
and lets the real secrets come from the vault.

Failure posture: a missing secret is skipped (same "missing lane" philosophy),
and by default a vault/credential error is logged and the app continues on the
environment alone. Set AZURE_KEY_VAULT_STRICT=1 to make any load failure fatal
(recommended in production, where a half-loaded secret set should not boot).
"""
import logging
import os

log = logging.getLogger("rivalyze.keyvault")

# Environment-variable names that may be sourced from Key Vault. Each is fetched
# from the vault under the same name with underscores replaced by hyphens
# (Key Vault secret names allow only [0-9A-Za-z-]), e.g. GEMINI_API_KEY ->
# secret "GEMINI-API-KEY", DATABASE_URL -> "DATABASE-URL".
SECRET_KEYS: tuple[str, ...] = (
    # LLM lanes
    "GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY",
    # search providers
    "TAVILY_API_KEY", "SERPER_API_KEY",
    # persistence
    "DATABASE_URL", "REDIS_URL",
    # API surface / user auth
    "BEARER_TOKEN", "JWT_SECRET",
)


def kv_secret_name(env_name: str) -> str:
    """Map an env-var name to its Key Vault secret name (underscores -> hyphens)."""
    return env_name.replace("_", "-")


def load_into_env() -> None:
    """Populate os.environ from Key Vault for any SECRET_KEYS not already set.

    No-op when AZURE_KEY_VAULT_URL is unset. Never raises unless
    AZURE_KEY_VAULT_STRICT=1, in which case a load failure is fatal.
    """
    vault_url = os.getenv("AZURE_KEY_VAULT_URL")
    if not vault_url:
        return  # offline / local / MOCK — nothing to do, azure libs never imported

    strict = os.getenv("AZURE_KEY_VAULT_STRICT") == "1"

    try:
        from azure.core.exceptions import ResourceNotFoundError
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as e:  # azure extras not installed
        msg = f"AZURE_KEY_VAULT_URL is set but azure packages are missing ({e})"
        if strict:
            raise RuntimeError(msg) from e
        log.error("%s — using environment only", msg)
        return

    try:
        client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
    except Exception as e:  # credential / URL construction failure
        if strict:
            raise
        log.error("Key Vault client init failed (%s) — using environment only", e)
        return

    loaded: list[str] = []
    missing: list[str] = []
    for env_name in SECRET_KEYS:
        if os.getenv(env_name):  # explicit env / .env wins — never overwrite
            continue
        try:
            value = client.get_secret(kv_secret_name(env_name)).value
        except ResourceNotFoundError:
            missing.append(env_name)
            continue
        except Exception as e:
            if strict:
                raise
            log.error("Key Vault fetch failed for %s (%s)", env_name, e)
            missing.append(env_name)
            continue
        if value is not None:
            os.environ[env_name] = value
            loaded.append(env_name)

    log.info(
        "Key Vault: loaded %d secret(s) [%s]; %d not present in vault",
        len(loaded), ", ".join(loaded) or "-", len(missing),
    )
