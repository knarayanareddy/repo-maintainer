#!/usr/bin/env python3
"""Send structured HTML execution summary to Telegram."""

import html
import json
import os
import sys
import urllib.request


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram notification skipped: missing credentials.")
        return 0

    title = sys.argv[1] if len(sys.argv) > 1 else "GitHub Actions Workflow"
    logfile = sys.argv[2] if len(sys.argv) > 2 else "curate.log"
    status = os.environ.get("JOB_STATUS", "completed")
    target = os.environ.get("REPO_TARGET", "all")
    trigger = os.environ.get("TRIGGER_NAME", "workflow")

    summary = "No log captured."
    if os.path.exists(logfile):
        try:
            with open(logfile, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                summary = "".join(lines[-10:]).strip()
        except Exception as e:
            summary = f"Error reading log: {e}"

    has_failed = (status != "success") or ("failed" in summary.lower() and "0 failed" not in summary.lower())
    icon = "❌" if has_failed else "✅"
    display_status = "failure" if has_failed else status

    safe_title = html.escape(title)
    safe_status = html.escape(display_status)
    safe_target = html.escape(target)
    safe_trigger = html.escape(trigger)
    safe_summary = html.escape(summary)

    text = (
        f"{icon} <b>GitHub Actions: {safe_title}</b>\n\n"
        f"<b>Status:</b> {safe_status}\n"
        f"<b>Target:</b> <code>{safe_target}</code>\n"
        f"<b>Trigger:</b> {safe_trigger}\n\n"
        f"<pre>{safe_summary}</pre>"
    )

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print("Telegram notification delivered successfully.")
    except Exception as e:
        print(f"Failed to deliver Telegram notification: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
