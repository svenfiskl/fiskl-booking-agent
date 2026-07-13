"""
Fiskl AI Booking Agent
======================
Automatischer Buchungsagent mit Claude Tool Use.

Umgebungsvariablen:
  ANTHROPIC_API_KEY     Pflicht
  FISKL_CREDENTIALS     JSON: {client_id: {token, base_url, auth_type}}
  DRY_RUN               true/false (default: false)
  BOOKING_DATE          YYYY-MM-DD (default: heute)
  CLIENT_FILTER         Nur diesen Client ausfuehren
  SLACK_WEBHOOK_URL     Optional
  LOG_LEVEL             DEBUG/INFO/WARNING
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anthropic
import httpx
import yaml

# --- Logging ---
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"agent_{date.today().isoformat()}.log"
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("fiskl-agent")


def load_config() -> dict:
    with open("config/booking_rules.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def last_day_of_month(y: int, m: int) -> int:
    return monthrange(y, m)[1]


def is_due_today(rule: dict, target: date) -> bool:
    d, m, y = target.day, target.month, target.year
    dom = rule["day_of_month"]
    if dom == "last":
        due_day = last_day_of_month(y, m)
    else:
        due_day = min(int(dom), last_day_of_month(y, m))
    day_matches = d == due_day
    if rule["frequency"] == "monthly":
        return day_matches
    if rule["frequency"] == "quarterly":
        return day_matches and m in (rule.get("months") or [])
    if rule["frequency"] == "yearly":
        return day_matches and m == rule.get("month", 1)
    return False


def get_due_rules(rules: list[dict], target: date) -> list[dict]:
    return [r for r in rules if is_due_today(r, target)]


class FisklClient:
    def __init__(self, client_id: str, creds: dict, dry_run: bool):
        self.client_id = client_id
        self.base_url = creds["base_url"].rstrip("/")
        self.token = creds.get("token", "")
        self.dry_run = dry_run
        cookies = {}
        if creds.get("auth_type") == "session":
            cookies["session"] = self.token
        self.http = httpx.Client(
            timeout=15.0,
            headers={"Content-Type": "application/json"},
            cookies=cookies,
        )

    def create_transaction(self, holder_account_id, account_id, amount_eur, name, occur_date, income=False):
        signed = round(abs(amount_eur) * 100) * (1 if income else -1)
        payload = {
            "id": None, "virtualId": None,
            "holderAccount": {"id": holder_account_id},
            "occurDate": occur_date,
            "amount": signed,
            "name": name,
            "account": {"id": account_id},
        }
        if self.dry_run:
            log.info("[DRY RUN] %s  %+.2f EUR", name, amount_eur if income else -amount_eur)
            return {"id": f"dry-{hash(name)}", "dry_run": True}
        resp = self.http.post(f"{self.base_url}/account-transaction", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_stripe_revenue(self, month: int, year: int):
        try:
            first = date(year, month, 1).isoformat()
            last = date(year, month, last_day_of_month(year, month)).isoformat()
            resp = self.http.get(
                f"{self.base_url}/account-transaction/list/603774",
                params={"start": first, "end": last},
            )
            if resp.status_code == 200:
                return sum(t["amount"] / 100 for t in resp.json() if t.get("amount", 0) > 0)
        except Exception as e:
            log.warning("Stripe-Umsatz nicht abrufbar: %s", e)
        return None


TOOLS = [
    {
        "name": "book_transaction",
        "description": "Fuehrt eine Buchung in Fiskl aus. Nur aufrufen wenn heute faellig.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string"},
                "name": {"type": "string"},
                "amount": {"type": "number"},
                "account_id": {"type": "integer"},
                "income": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["rule_id", "name", "amount", "account_id", "income", "reason"],
        },
    },
    {
        "name": "skip_booking",
        "description": "Ueberspringt eine faellige Buchung.",
        "input_schema": {
            "type": "object",
            "properties": {"rule_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["rule_id", "reason"],
        },
    },
    {
        "name": "flag_anomaly",
        "description": "Meldet eine Anomalie zur manuellen Pruefung.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["message", "severity"],
        },
    },
]


@dataclass
class BookingResult:
    rule_id: str
    name: str
    amount: float
    income: bool
    status: str
    fiskl_id: str = ""
    reason: str = ""
    error: str = ""


def run_agent_for_client(client_id, client_cfg, rules, creds, target_date, dry_run, ant_client):
    log.info("=" * 60)
    log.info("Client: %s | Datum: %s | DryRun: %s", client_cfg["name"], target_date, dry_run)

    due_rules = get_due_rules(rules, target_date)
    if not due_rules:
        log.info("Keine Buchungen heute faellig.")
        return []

    log.info("%d Buchungen faellig: %s", len(due_rules), [r["id"] for r in due_rules])

    fiskl = FisklClient(client_id, creds, dry_run)
    prev = (target_date.replace(day=1) - timedelta(days=1))
    stripe_revenue = fiskl.get_stripe_revenue(prev.month, prev.year)

    for rule in due_rules:
        if rule.get("dynamic") and not rule.get("amount"):
            formula = rule.get("dynamic_formula", "")
            try:
                ctx = {
                    "stripe_revenue": stripe_revenue or 0,
                    "stripe_payout_amount": stripe_revenue or 0,
                    "prev_month_revenue": stripe_revenue or 0,
                }
                rule["amount"] = round(eval(formula, {"__builtins__": {}}, ctx), 2)
            except Exception:
                rule["amount"] = rule.get("amount_fallback", 0)

    m, y = target_date.month, target_date.year
    system = f"""Du bist ein Buchungsagent fuer {client_cfg['name']}.
Heute: {target_date}. Modus: {'DRY RUN' if dry_run else 'LIVE'}.
Arbeite alle faelligen Buchungen ab. Beginne mit Prioritaet HIGH."""

    user_msg = f"""Datum: {target_date.isoformat()}
Stripe-Umsatz Vormonat: {f'{stripe_revenue:,.2f} EUR' if stripe_revenue else 'n/a'}

Faellige Buchungen:
{json.dumps([{
    'id': r['id'], 'name': r['name'], 'amount': r.get('amount'),
    'income': r.get('income', False), 'account_id': r['account_id'],
    'priority': r['priority']
} for r in due_rules], ensure_ascii=False, indent=2)}"""

    results = []
    messages = [{"role": "user", "content": user_msg}]

    for _ in range(20):
        response = ant_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue
            inp = block.input
            if block.name == "book_transaction":
                rule = next((r for r in due_rules if r["id"] == inp["rule_id"]), None)
                try:
                    tx = fiskl.create_transaction(
                        holder_account_id=client_cfg["holder_account_id"],
                        account_id=inp["account_id"],
                        amount_eur=inp["amount"],
                        name=f"{inp['name']} {m:02d}/{y}",
                        occur_date=target_date.isoformat(),
                        income=inp["income"],
                    )
                    fiskl_id = tx.get("id", "?")
                    log.info("OK  %s  %s%.2f EUR (ID: %s)",
                             inp["name"], "+" if inp["income"] else "-", inp["amount"], fiskl_id)
                    results.append(BookingResult(
                        rule_id=inp["rule_id"], name=inp["name"],
                        amount=inp["amount"], income=inp["income"],
                        status="booked" if not dry_run else "dry_run",
                        fiskl_id=str(fiskl_id), reason=inp.get("reason", ""),
                    ))
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                         "content": f"Gebucht. ID: {fiskl_id}"})
                except Exception as e:
                    log.error("FEHLER %s: %s", inp["name"], e)
                    results.append(BookingResult(
                        rule_id=inp["rule_id"], name=inp["name"],
                        amount=inp["amount"], income=inp["income"],
                        status="error", error=str(e),
                    ))
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                         "content": f"Fehler: {e}", "is_error": True})

            elif block.name == "skip_booking":
                log.info("SKIP %s (%s)", inp["rule_id"], inp["reason"])
                results.append(BookingResult(
                    rule_id=inp["rule_id"], name=inp["rule_id"],
                    amount=0, income=False, status="skipped", reason=inp["reason"],
                ))
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": "Uebersprungen."})

            elif block.name == "flag_anomaly":
                log.warning("ANOMALIE [%s]: %s", inp["severity"].upper(), inp["message"])
                results.append(BookingResult(
                    rule_id="anomaly", name="Anomalie",
                    amount=0, income=False, status="anomaly", reason=inp["message"],
                ))
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": "Anomalie gemeldet."})

        if tool_results:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        if response.stop_reason == "end_turn":
            break

    return results


def send_slack(webhook_url, results, client_name, target_date, dry_run):
    booked = [r for r in results if r.status in ("booked", "dry_run")]
    errors = [r for r in results if r.status == "error"]
    total = sum((-r.amount if not r.income else r.amount) for r in booked)
    icon = "ERR" if errors else ("DRY" if dry_run else "OK")
    text = f"[{icon}] {client_name} | {target_date} | {len(booked)} Buchungen | {total:+,.2f} EUR"
    if errors:
        text += f" | FEHLER: {', '.join(r.name for r in errors)}"
    try:
        httpx.post(webhook_url, json={"text": text}, timeout=5)
    except Exception as e:
        log.warning("Slack fehlgeschlagen: %s", e)


def write_summary(results, client_id, target_date, dry_run):
    path = LOG_DIR / f"summary_{target_date.isoformat()}.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[client_id] = {
        "date": target_date.isoformat(),
        "dry_run": dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "booked": len([r for r in results if r.status in ("booked", "dry_run")]),
            "skipped": len([r for r in results if r.status == "skipped"]),
            "errors": len([r for r in results if r.status == "error"]),
        },
        "bookings": [{"rule_id": r.rule_id, "name": r.name, "amount": r.amount,
                      "income": r.income, "status": r.status, "fiskl_id": r.fiskl_id} for r in results],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def main():
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    date_str = os.getenv("BOOKING_DATE", "").strip()
    client_filter = os.getenv("CLIENT_FILTER", "").strip()
    target_date = date.fromisoformat(date_str) if date_str else date.today()

    log.info("Fiskl AI Booking Agent | %s | DryRun=%s", target_date, dry_run)

    config = load_config()
    rules = config["booking_rules"]

    # GitHub Actions setzt nicht konfigurierte Secrets als LEEREN String,
    # nicht als fehlende Variable — daher explizit auf "leer" pruefen.
    creds_raw = os.getenv("FISKL_CREDENTIALS", "").strip() or "{}"
    try:
        all_creds = json.loads(creds_raw)
    except json.JSONDecodeError as e:
        log.critical(
            "Secret FISKL_CREDENTIALS enthaelt kein gueltiges JSON (%s). "
            "Bitte pruefen: Settings -> Secrets and variables -> Actions.", e
        )
        sys.exit(1)
    if not all_creds:
        log.warning(
            "Secret FISKL_CREDENTIALS ist nicht gesetzt oder leer — "
            "es koennen keine echten Buchungen erfolgen (nur DRY_RUN)."
        )

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.critical(
            "Secret ANTHROPIC_API_KEY ist nicht gesetzt. "
            "Bitte anlegen unter: Settings -> Secrets and variables -> Actions "
            "-> New repository secret."
        )
        sys.exit(1)
    ant_client = anthropic.Anthropic(api_key=api_key)

    slack_url = os.getenv("SLACK_WEBHOOK_URL", "")
    exit_code = 0

    for client_id, client_cfg in config["clients"].items():
        if not client_cfg.get("active"):
            continue
        if client_filter and client_id != client_filter:
            continue
        creds = all_creds.get(client_id, {})
        if not creds and not dry_run:
            log.error("Keine Credentials fuer %s!", client_id)
            exit_code = 1
            continue
        if dry_run and not creds:
            creds = {"base_url": client_cfg["base_url"], "token": "dry-run"}
        try:
            results = run_agent_for_client(
                client_id, client_cfg, rules, creds,
                target_date, dry_run, ant_client,
            )
            write_summary(results, client_id, target_date, dry_run)
            if slack_url:
                send_slack(slack_url, results, client_cfg["name"], target_date, dry_run)
            if any(r.status == "error" for r in results):
                exit_code = 1
        except Exception:
            log.critical("Fehler fuer %s:\n%s", client_id, traceback.format_exc())
            exit_code = 1

    log.info("Fertig. Exit-Code: %d", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Sicherstellen, dass unerwartete Fehler auch im Log-Artefakt landen
        log.critical("Unbehandelter Fehler:\n%s", traceback.format_exc())
        sys.exit(1)
