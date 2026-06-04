# Fiskl AI Booking Agent

Täglicher Claude-Agent der automatisch Buchungen in Fiskl erstellt.

---

## Schnellstart (Sandbox testen)

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY="sk-ant-..."
export FISKL_CREDENTIALS='{"kifi_sandbox": {"base_url": "https://sandbox.fiskl.ca/api", "token": "DEIN-SESSION-TOKEN", "auth_type": "session"}}'
export DRY_RUN=true

python src/agent.py
```

---

## GitHub Actions einrichten (5 Minuten)

### 1. Secrets setzen
`Settings → Secrets → Actions → New repository secret`

| Secret | Inhalt |
|--------|--------|
| `ANTHROPIC_API_KEY` | `sk-ant-api...` |
| `FISKL_CREDENTIALS` | JSON (siehe unten) |
| `SLACK_WEBHOOK_URL` | Optional |

**FISKL_CREDENTIALS Format:**
```json
{
  "kifi_sandbox": {
    "base_url": "https://sandbox.fiskl.ca/api",
    "token": "DEIN-SESSION-TOKEN",
    "auth_type": "session"
  }
}
```

### 2. Workflow aktivieren
`Actions → Fiskl AI Booking Agent → Enable workflow`

Läuft täglich um **06:15 Uhr MEZ** automatisch.

---

## Manuell starten

`Actions → Fiskl AI Booking Agent → Run workflow`

- **Dry Run**: `true` = simulieren, `false` = echte Buchungen
- **Booking Date**: leer = heute, oder `2026-06-15` für Backfill
- **Client Filter**: leer = alle

---

## Architektur

```
GitHub Actions Cron (06:15 MEZ)
        │
        ▼
   src/agent.py
        │
        ├── config/booking_rules.yaml
        │
        ├── Claude API (Tool Use)
        │       book_transaction()
        │       skip_booking()
        │       flag_anomaly()
        │
        └── Fiskl API
                POST /api/account-transaction
```

Für Produktions-Kunden: Fiskl OAuth2-Token statt Session-Cookie.
→ Fiskl API-Docs: https://developers.fiskl.com/oauth2
