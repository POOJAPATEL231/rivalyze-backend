# Secrets via Azure Key Vault

Secrets (LLM keys, `DATABASE_URL`, `REDIS_URL`, `BEARER_TOKEN`, `JWT_SECRET`)
live in **Azure Key Vault** in any deployed run — not in a `.env` file. At
startup `app/__init__.py` calls `app/core/keyvault.py::load_into_env()`, which
pulls the secrets into the process environment **before** `core/config.py` reads
them, so no other code changes.

## How it decides where secrets come from
- `AZURE_KEY_VAULT_URL` **unset** → no-op; the app uses `.env` / environment
  only (local, MOCK, offline, CI). The `azure-*` packages aren't even imported.
- `AZURE_KEY_VAULT_URL` **set** → each secret in `SECRET_KEYS` that is **not
  already in the environment** is fetched from the vault. An existing env value
  always wins (local override / emergency break-glass).
- `AZURE_KEY_VAULT_STRICT=1` → a vault/credential failure is **fatal** (the app
  refuses to boot half-configured). Recommended in production.

Secret naming: env `GEMINI_API_KEY` ↔ vault secret `GEMINI-API-KEY`
(underscores → hyphens, since Key Vault names allow only `[0-9A-Za-z-]`).

Authentication is `DefaultAzureCredential`: Managed Identity in Azure, and
`az login` (or `AZURE_CLIENT_ID/SECRET/TENANT_ID`) locally.

---

## One-time Azure setup

### 1. Create the vault (RBAC authorization model)
```bash
az group create -n rivalyze-rg -l centralindia
az keyvault create -n rivalyze-kv -g rivalyze-rg -l centralindia \
  --enable-rbac-authorization true
```

### 2. Let yourself write secrets, then upload them
```bash
ME=$(az ad signed-in-user show --query id -o tsv)
VAULT_ID=$(az keyvault show -n rivalyze-kv --query id -o tsv)
az role assignment create --assignee "$ME" \
  --role "Key Vault Secrets Officer" --scope "$VAULT_ID"

# set secrets (hyphenated names)
az keyvault secret set --vault-name rivalyze-kv --name GEMINI-API-KEY   --value "..."
az keyvault secret set --vault-name rivalyze-kv --name GROQ-API-KEY     --value "..."
az keyvault secret set --vault-name rivalyze-kv --name TAVILY-API-KEY   --value "..."
az keyvault secret set --vault-name rivalyze-kv --name DATABASE-URL     --value "postgresql://..."
az keyvault secret set --vault-name rivalyze-kv --name REDIS-URL        --value "rediss://..."
az keyvault secret set --vault-name rivalyze-kv --name BEARER-TOKEN     --value "$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
az keyvault secret set --vault-name rivalyze-kv --name JWT-SECRET       --value "$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

### 3. Give the App Service a Managed Identity + read access
```bash
az webapp identity assign -g rivalyze-rg -n RIVALYZE-APP-NAME
APP_MI=$(az webapp identity show -g rivalyze-rg -n RIVALYZE-APP-NAME --query principalId -o tsv)
az role assignment create --assignee "$APP_MI" \
  --role "Key Vault Secrets User" --scope "$VAULT_ID"   # read-only: get/list secrets
```

### 4. Point the app at the vault (the ONLY secret-ish app setting)
```bash
az webapp config appsettings set -g rivalyze-rg -n RIVALYZE-APP-NAME --settings \
  AZURE_KEY_VAULT_URL="https://rivalyze-kv.vault.azure.net/" \
  AZURE_KEY_VAULT_STRICT="1"
```
No API keys, DB URL, or JWT secret go into App Settings anymore.

---

## Local development
Two options:
- **No vault (simplest):** keep a local `.env` with the values. `AZURE_KEY_VAULT_URL`
  stays blank → the loader is a no-op.
- **Against the real vault:** `az login`, grant your user `Key Vault Secrets User`
  on the vault, then set `AZURE_KEY_VAULT_URL` and leave the secrets blank.

## Rotation
Secrets are read once at startup. After rotating a value in the vault, **restart**
the app (or roll the instances) to pick it up. A periodic in-process refresh is a
possible future enhancement; not needed for the current single-restart model.

## Migrate an existing `.env` to the vault (PowerShell helper)
```powershell
$vault = "rivalyze-kv"
Get-Content .env | Where-Object { $_ -match "^\s*[A-Z].*=" -and $_ -notmatch "^\s*#" } | ForEach-Object {
  $name, $value = $_ -split "=", 2
  if ($value.Trim()) {
    $secret = $name.Trim().Replace("_", "-")
    az keyvault secret set --vault-name $vault --name $secret --value $value.Trim() | Out-Null
    Write-Host "uploaded $secret"
  }
}
```
