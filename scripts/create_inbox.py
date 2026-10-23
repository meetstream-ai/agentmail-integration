"""Create the agent's AgentMail inbox.

    python scripts/create_inbox.py my-notetaker

Prints the new address; put it in .env as AGENTMAIL_INBOX_ID. Needs only
AGENTMAIL_API_KEY (in .env or the environment).
"""

import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def api_key() -> str:
    key = os.environ.get("AGENTMAIL_API_KEY")
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("AGENTMAIL_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("Set AGENTMAIL_API_KEY in .env first (https://console.agentmail.to).")
    return key


def own_inbox(username: str, key: str, http: httpx.Client) -> str | None:
    """The address of this account's inbox with that username, if it has one."""
    resp = http.get(
        "https://api.agentmail.to/v0/inboxes", headers={"Authorization": f"Bearer {key}"}, params={"limit": 100}
    )
    if resp.status_code >= 400:
        return None
    ids = [i.get("inbox_id", "") for i in resp.json().get("inboxes", [])]
    return next((i for i in ids if i.split("@")[0].lower() == username.lower()), None)


def create_inbox(username: str, key: str, http: httpx.Client) -> tuple[str, bool]:
    """The inbox's address, and whether it was created just now."""
    resp = http.post(
        "https://api.agentmail.to/v0/inboxes",
        headers={"Authorization": f"Bearer {key}"},
        json={"username": username, "display_name": "AgentMail Notetaker"},
    )
    if resp.status_code >= 400:
        if "already_exists" in resp.text:
            mine = own_inbox(username, key, http)
            if mine:
                return mine, False  # running the script twice is fine
            sys.exit(f'"{username}" is taken by someone else. Pick another name.')
        sys.exit(f"AgentMail said no ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["inbox_id"], True


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/create_inbox.py <username>   e.g. my-notetaker")
    with httpx.Client(timeout=30) as http:
        address, created = create_inbox(sys.argv[1], api_key(), http)
    print(f"{'Created' if created else 'You already have'} {address}\nPut this in .env:\nAGENTMAIL_INBOX_ID={address}")


if __name__ == "__main__":
    main()
