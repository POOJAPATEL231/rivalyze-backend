# Frontend Integration — "By the Numbers" strip (`report.stats`)

The report now carries a **deterministic stats block**: every value is a real count of
the evidence/signals we gathered — nothing estimated. Render it as a horizontal strip
of stat cards under the executive summary. Demo line:
> *"Every number here is a count of evidence we gathered — 58% corroborated means 58%
> of findings have two or more **independent** sources. Click any claim to verify."*

---

## 0. Where it lives

It's an **additive, optional** field on the existing report:

```
GET /api/v1/reports/{run_id}    (Bearer auth)
-> CompetitiveReport {
     company, threat_level, executive_summary, swot, sentiment,
     head_to_head, opportunities, recommendations, low_signal_findings,
     analysis_date,
     stats: ReportStats | null      // <-- NEW
   }
```

No new endpoint. Same report you already fetch — just read `report.stats`.

### ⚠️ Rule #1: `stats` can be `null`
It's `null` on **degraded runs** (synthesis failed) and on **older reports** created
before this feature. **Always null-check and render nothing if absent** — never assume
it exists:
```js
if (report.stats) renderStatsStrip(report.stats);
```

---

## 1. The shape

```jsonc
"stats": {
  "evidence_count":        14,                     // int  — total evidence rows
  "competitors_analyzed":  4,                       // int
  "sources_per_competitor": {                       // {rival: count} — bar per rival
      "Swiggy": 5, "Dineout": 4, "Burrp": 3, "Uber Eats": 2 },
  "source_type_breakdown": {                         // {type: count} — the donut
      "news": 5, "pricing": 4, "review": 3, "web": 2, "document": 0 },
  "signals_by_type": {                              // {type: count}
      "pricing": 4, "launch": 3, "complaint": 6, "funding": 1 },
  "competitors_with_complaints": 3,                 // int  — always <= competitors_analyzed
  "sentiment_spread": {                             // 3 fixed keys — mini-bars
      "POSITIVE": 1, "NEUTRAL": 1, "NEGATIVE": 2 },
  "avg_confidence":       0.62,                     // float 0-1  OR null (no recs)
  "freshest_signal_days": 3,                        // int  OR null (no dated evidence)
  "distinct_sources":     9,                        // int  — independent domains
  "corroboration_rate":   58,                       // int PERCENT 0-100  OR null (no claims)
  "uncorroborated_claims": 5                        // int  — findings on a single source
}
```

### ⚠️ Rule #2: units differ between two fields — don't multiply blindly
| field | unit | render as |
|---|---|---|
| `corroboration_rate` | **integer percent 0–100** (or `null`) | `58%` — use the number **directly**, do **not** ×100 |
| `avg_confidence` | **fraction 0–1** (or `null`) | multiply ×100 for a %, e.g. `0.62 → 62%` |

### ⚠️ Rule #3: three fields are nullable
`avg_confidence`, `freshest_signal_days`, `corroboration_rate` are `null` when they have
no data (no recs / no dated evidence / no claims). Render a dash (`—`) or hide the card —
`null` is **not** an error and **not** zero.

---

## 2. Field-by-field rendering guide

| Card | Source | Example copy |
|---|---|---|
| **Evidence sources** | `evidence_count` | `14 EVIDENCE SOURCES` |
| **Competitors** | `competitors_analyzed` | `4 COMPETITORS` |
| **Corroboration** (hero honesty stat) | `corroboration_rate` | `58% CORROBORATED (2+ sources)` — hide if `null` |
| **Independent domains** | `distinct_sources` | `9 INDEPENDENT DOMAINS` |
| **Single-source caveat** | `uncorroborated_claims` | `5 findings on a single source` (muted/amber) |
| **Source mix (donut)** | `source_type_breakdown` | donut of news/pricing/review/web/document |
| **Sentiment** | `sentiment_spread` | 3 mini-bars: 🟢 POSITIVE 🟡 NEUTRAL 🔴 NEGATIVE |
| **Complaints coverage** | `competitors_with_complaints` / `competitors_analyzed` | `3 of 4 rivals have complaints` |
| **Sources per rival** | `sources_per_competitor` | small bar chart, one bar per rival |
| **Freshness** | `freshest_signal_days` | `newest evidence: 3 days old` — hide if `null` |
| **Avg confidence** | `avg_confidence` (×100) | `62% avg recommendation confidence` — hide if `null` |

Notes:
- **`source_type_breakdown` always sums to `evidence_count`** — safe to compute donut
  percentages as `count / evidence_count`. The 5 keys are always present (a slice may be 0).
- **`sentiment_spread` always has all 3 keys** (`POSITIVE`/`NEUTRAL`/`NEGATIVE`, 0 if none) —
  stable bars, no missing-key handling needed.
- `sources_per_competitor` / `signals_by_type` have **dynamic keys** — iterate, don't
  hard-code rival or type names.

---

## 3. Minimal render sketch

```jsx
function StatsStrip({ stats }) {
  if (!stats) return null;                                  // Rule #1

  const pct = (n) => (n == null ? "—" : `${n}%`);          // corroboration_rate is already %
  const conf = stats.avg_confidence == null
      ? "—" : `${Math.round(stats.avg_confidence * 100)}%`; // avg_confidence is 0-1  (Rule #2)

  return (
    <div className="stats-strip">
      <Card big={stats.evidence_count}        label="EVIDENCE SOURCES" />
      <Card big={stats.competitors_analyzed}  label="COMPETITORS" />
      {stats.corroboration_rate != null &&
        <Card big={pct(stats.corroboration_rate)} label="CORROBORATED (2+ sources)" hero />}
      <Card big={stats.distinct_sources}      label="INDEPENDENT DOMAINS" />
      <Donut data={stats.source_type_breakdown} total={stats.evidence_count} />
      <SentimentBars data={stats.sentiment_spread} />
      {stats.uncorroborated_claims > 0 &&
        <Note tone="amber">{stats.uncorroborated_claims} findings on a single source</Note>}
    </div>
  );
}
```

---

## 4. What corroboration actually means (for the demo / a judge question)

`corroboration_rate` = the share of distinct claims backed by **2 or more independent
source domains**. Two citations from the *same* domain count as **one** source (we strip
scheme + `www` and de-dupe by host), so the number can't be inflated by re-citing one
page. `uncorroborated_claims` is the honest complement — findings that currently rest on
a single source. This pairs with the existing citation drawer: every stat is a count of
rows the user can click through and verify.

---

## 5. Empty / thin run

A thin run just yields small numbers — never an error, never a blank:
`evidence_count: 1`, `competitors_analyzed: 1`, nullable rates `null`. Render the strip
exactly the same; cards show `1`, `—`, etc. Don't special-case it.
