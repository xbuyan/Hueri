#!/usr/bin/env python3
"""One-shot end-to-end verification of SMS alert delivery.

Proves the SMS gateway credentials work before relying on pipeline alerts:

    .venv/bin/python scripts/verify_sms.py +2547XXXXXXXX [--text "..."]

Requires SMS_API_URL (and SMS_HEADERS / SMS_PAYLOAD_TEMPLATE when needed)
in the environment or .env / .env.production. Note the notifier treats
`ENVIRONMENT != "production"` as dev mode — but this script bypasses that
gate on purpose: it tests the credentials directly.

Exit codes: 0 = delivered (AT "Success" or unknown body contract),
1 = gateway rejected / HTTP error / not configured.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env_file(path: str) -> None:
    """Minimal KEY=VALUE loader so the script can read .env.production
    without adding a python-dotenv dependency. Runs before app imports
    (Settings reads the environment at import time)."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Load .env first (matches config.py), then production overrides if present.
_load_env_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
_load_env_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.production"))

from app.config import settings  # noqa: E402
from app.services.notifier import notifier  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phone", help="Destination in E.164, e.g. +2547XXXXXXXX")
    parser.add_argument(
        "--text",
        default="HUERI TenderScout: SMS alert verification message. Please ignore.",
    )
    args = parser.parse_args()

    if not settings.SMS_API_URL:
        print("ERROR: SMS_API_URL is not set.", file=sys.stderr)
        print(
            "Set it in .env.production (or export it), e.g.:\n"
            '  SMS_API_URL="https://api.sandbox.africastalking.com/version1/messaging"\n'
            '  SMS_PAYLOAD_TEMPLATE=\'{"username":"sandbox","to":["{to}"],"message":"{message}"}\'\n'
            '  SMS_HEADERS=\'{"apiKey":"AT-KEY","Content-Type":"application/json","Accept":"application/json"}\'',
            file=sys.stderr,
        )
        return 1

    print(f"Sending to {args.phone} via {settings.SMS_API_URL} ...")
    try:
        body = await notifier.send_test_sms(args.phone, args.text)
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    recipients = (body or {}).get("SMSMessageData", {}).get("Recipients") or []
    if recipients:
        for r in recipients:
            print(
                f"  {r.get('status')}: {r.get('phoneNumber')} "
                f"messageId={r.get('messageId')} cost={r.get('cost')}"
            )
        statuses = {str(r.get("status")) for r in recipients}
        if statuses != {"Success"}:
            return 1
    else:
        print(f"  Gateway accepted (HTTP 2xx). Response body: {body!r}")
    print("OK — check the phone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
