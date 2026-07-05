# Frontend — expanded `/analyze/idea` intake

`POST /api/v1/analyze/idea` now accepts **optional structured fields** so a founder can
pin down the market instead of the backend guessing everything from one sentence. The
request is **fully backward-compatible**: a bare `{ "idea": "..." }` body behaves exactly
as before.

## Request body

```jsonc
POST /api/v1/analyze/idea      (Bearer auth)
{
  "idea": "an app for dog walkers to schedule visits and take payments",  // REQUIRED
  "industry":         "pet services",              // optional
  "target_geography": "Ahmedabad, India",          // optional  <-- drives LOCAL rivals
  "target_customer":  "B2C pet owners",            // optional
  "business_model":   "subscription marketplace",  // optional
  "stage":            "MVP"                          // optional
}
```

| field | required | max len | purpose |
|---|---|---|---|
| `idea` | ✅ yes (non-blank) | 500 | the free-text idea (unchanged) |
| `industry` | no | 120 | market/space — seeds the search category |
| `target_geography` | no | 120 | city/region/country → **discovery prefers rivals in that market** |
| `target_customer` | no | 120 | B2B / B2C / segment |
| `business_model` | no | 120 | subscription / marketplace / ads / … |
| `stage` | no | 60 | idea / MVP / launched |

All optional fields default to `""`; send only the ones you have. Control characters are
stripped server-side. **Response is unchanged** — `{ job_id, status: "running_discovery" }`,
then poll `GET /api/v1/runs/{job_id}` exactly as documented in the main integration guide
(competitors appear at `awaiting_confirmation` under `result.competitors`).

## Why `target_geography` matters most
The idea pre-step folds geography into the resolved market definition, so competitor
discovery surfaces **same-market** players. e.g. an Ahmedabad/India idea returns Indian
& regional rivals rather than US-only ones. The other fields refine the market phrase
(sharper, more relevant competitors); geography changes *which* rivals you get.

## Validation (what the API rejects)
- Missing or blank `idea` → **422** (`{"idea": "   "}` or omitting it entirely).
- Any optional field alone, without `idea` → **422** (idea is still required).
- Everything else is accepted; unknown extra keys are ignored.

## Suggested form UX
Show `idea` as the one required field, with the five optional fields behind an
**"Add details (optional)"** expander. Prefill nothing; every field is genuinely
optional. A short helper under `target_geography` — *"Where will you operate? We'll
prioritise competitors in that market."* — nudges the highest-value input.

## Minimal call
```js
await fetch(`${BASE}/analyze/idea`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
  body: JSON.stringify({
    idea,
    ...(industry        && { industry }),
    ...(geography       && { target_geography: geography }),
    ...(customer        && { target_customer: customer }),
    ...(model           && { business_model: model }),
    ...(stage           && { stage }),
  }),
});
```
