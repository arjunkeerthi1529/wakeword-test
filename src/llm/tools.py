"""Tool definitions and executor for the Jarvis voice agent loop.

The LLM decides which tool to call; execute_tool() runs it and returns
a JSON result string that gets fed back as a ``tool`` message.
"""
import json
import logging
from datetime import date

import requests

logger = logging.getLogger(__name__)

JARVIS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "log_expense",
            "description": "Log a purchase or expense to the financial tracker",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "description": "Amount in rupees",
                    },
                    "merchant": {
                        "type": "string",
                        "description": "Merchant or payee name",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "food_dining", "groceries", "transport", "shopping",
                            "health_fitness", "entertainment", "subscriptions",
                            "travel", "rent_utilities", "education",
                            "fees_charges", "other",
                        ],
                        "description": "Expense category",
                    },
                    "account": {
                        "type": "string",
                        "enum": ["bank", "credit_card"],
                        "description": "Which account to charge. Default: bank",
                    },
                },
                "required": ["amount", "merchant", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a reminder for the user after a delay or at a specific time",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "What to remind about",
                    },
                    "delay_seconds": {
                        "type": "number",
                        "description": "Fire after this many seconds from now",
                    },
                    "fires_at": {
                        "type": "string",
                        "description": "Absolute time in HH:MM format",
                    },
                },
                "required": ["message"],
            },
        },
    },
]


def execute_tool(
    name: str,
    args: dict,
    accounts: dict[str, str],
    financial_url: str,
    agent_url: str,
) -> str:
    """Run a tool call and return a JSON result string for the LLM."""
    if name == "log_expense":
        return _log_expense(args, accounts, financial_url)
    if name == "set_reminder":
        return _set_reminder(args, agent_url)
    return json.dumps({"status": "error", "detail": f"unknown tool: {name}"})


def _log_expense(args: dict, accounts: dict[str, str], financial_url: str) -> str:
    account_type = args.get("account", "bank")
    account_id = accounts.get(account_type) or next(iter(accounts.values()), None)
    if not account_id:
        return json.dumps({"status": "error", "detail": "no accounts configured"})

    amount = float(args.get("amount", 0))
    merchant = args.get("merchant", "Unknown")
    category = args.get("category", "other")

    payload = {
        "merchant": merchant,
        "amount_paise": round(amount * 100),
        "posted_date": date.today().isoformat(),
        "type": "expense",
        "category_id": category,
        "account_id": account_id,
        "source": "voice",
    }
    try:
        resp = requests.post(f"{financial_url}/transactions", json=payload, timeout=3)
        resp.raise_for_status()
        txn = resp.json()
        logger.info("Expense logged: %s ₹%.0f → %s (%s)", merchant, amount, account_type, txn.get("id", "?"))
        return json.dumps({"status": "ok", "merchant": merchant, "amount": amount, "account": account_type})
    except Exception as exc:
        logger.warning("Financial service error: %s", exc)
        return json.dumps({"status": "error", "detail": str(exc)})


def _set_reminder(args: dict, agent_url: str) -> str:
    from datetime import datetime, timedelta

    payload: dict = {"message": args["message"]}
    if "delay_seconds" in args and args["delay_seconds"]:
        payload["delay_s"] = int(args["delay_seconds"])
    elif "fires_at" in args and args["fires_at"]:
        raw = args["fires_at"]
        try:
            datetime.fromisoformat(raw)
            payload["fires_at"] = raw
        except ValueError:
            now = datetime.now()
            h, m = map(int, raw.split(":"))
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            payload["fires_at"] = target.isoformat(timespec="seconds")
    else:
        payload["delay_s"] = 300

    try:
        resp = requests.post(f"{agent_url}/remind", json=payload, timeout=3)
        resp.raise_for_status()
        logger.info("Reminder set via tool: %r", args["message"])
        return json.dumps({"status": "ok", "message": args["message"]})
    except Exception as exc:
        logger.warning("Agent service error: %s", exc)
        return json.dumps({"status": "error", "detail": str(exc)})
