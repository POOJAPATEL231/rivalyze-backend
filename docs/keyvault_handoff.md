# Key Vault handoff — DevOps checklist

The backend sources its secrets from Azure Key Vault at startup (see
`docs/azure_key_vault.md` for the mechanism). This page is the short list of
**what to create** and **how the app connects** — no app code is involved.

## How the app connects to Azure (no stored credentials)
The app uses **Managed Identity** via `DefaultAzureCredential` — there is NO
Azure connection string / client secret stored anywhere. The only Azure config
the app needs is one **non-secret** app setting pointing at the vault:

| App Setting (plain env var, NOT a secret) | Value |
|---|---|
| `AZURE_KEY_VAULT_URL` | `https://<vault-name>.vault.azure.net/` |
| `AZURE_KEY_VAULT_STRICT` | `1`  (fail-fast if the vault is unreachable) |

Auth chain: **App Service system-assigned Managed Identity → RBAC role
"Key Vault Secrets User" → Key Vault → secrets**. Locally, `az login` is used
instead of the Managed Identity.

## Secrets to create in the vault
Secret names use **hyphens** (Key Vault allows only `[0-9A-Za-z-]`). The app maps
env-var names to these automatically (`_` → `-`).

| Vault secret name | App env var | What it is | Criticality |
|---|---|---|---|
| `GEMINI-API-KEY`     | `GEMINI_API_KEY`     | Google Gemini LLM key         | ≥1 LLM key required |
| `GROQ-API-KEY`       | `GROQ_API_KEY`       | Groq LLM key                  | ≥1 LLM key required |
| `CEREBRAS-API-KEY`   | `CEREBRAS_API_KEY`   | Cerebras LLM key              | optional (extra lane) |
| `OPENROUTER-API-KEY` | `OPENROUTER_API_KEY` | OpenRouter LLM key            | optional (extra lane) |
| `TAVILY-API-KEY`     | `TAVILY_API_KEY`     | Tavily web-search key         | ≥1 search key required |
| `SERPER-API-KEY`     | `SERPER_API_KEY`     | Serper web-search key         | optional (fallback) |
| `DATABASE-URL`       | `DATABASE_URL`       | Postgres DSN (Azure Flexible) | **required** (persistence, auth) |
| `REDIS-URL`          | `REDIS_URL`          | Redis DSN (Azure Cache, TLS)  | optional (cache/counters) |
| `BEARER-TOKEN`       | `BEARER_TOKEN`       | Static service API token      | **required** in prod |
| `JWT-SECRET`         | `JWT_SECRET`         | HS256 signing key (user auth) | **required** in prod |

Notes:
- The LLM router skips any lane whose key is absent, so not every LLM/search key
  must exist — but provide **at least one LLM key and one search key**.
- `BEARER-TOKEN` and `JWT-SECRET` should be **generated random**, not chosen:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `DATABASE-URL` format: `postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require`
- `REDIS-URL` format (Azure Cache, TLS on 6380): `rediss://:PASSWORD@HOST:6380/0`

## Provisioning commands (Azure CLI)
```bash
# 1) Vault in RBAC mode
az keyvault create -n <vault-name> -g <rg> -l <region> --enable-rbac-authorization true

# 2) Grant whoever uploads secrets (you) write access
VAULT_ID=$(az keyvault show -n <vault-name> --query id -o tsv)
az role assignment create --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --role "Key Vault Secrets Officer" --scope "$VAULT_ID"

# 3) Create the secrets (repeat per row above; random-generate BEARER/JWT)
az keyvault secret set --vault-name <vault-name> --name GEMINI-API-KEY --value "..."
# ... etc for each secret name in the table ...

# 4) Give the App Service a Managed Identity and READ access to the vault
az webapp identity assign -g <rg> -n <app-name>
APP_MI=$(az webapp identity show -g <rg> -n <app-name> --query principalId -o tsv)
az role assignment create --assignee "$APP_MI" \
  --role "Key Vault Secrets User" --scope "$VAULT_ID"

# 5) Point the app at the vault (the only Azure setting the app needs)
az webapp config appsettings set -g <rg> -n <app-name> --settings \
  AZURE_KEY_VAULT_URL="https://<vault-name>.vault.azure.net/" \
  AZURE_KEY_VAULT_STRICT="1"
```

That's it — no secrets in App Settings, the repo, or a server-side `.env`.
```
