"""Point the agent inbox's message.received webhook at this server.

    python scripts/register_webhook.py

Prints the signing secret to put in .env as AGENTMAIL_WEBHOOK_SECRET.
Run it again after your public URL changes (e.g. a new tunnel URL). AgentMail
cannot change a webhook's URL, so this registers a new one and disables the
inbox's earlier ones, which would otherwise keep retrying a dead address.
"""

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()
    url = f"{settings.public_base_url.rstrip('/')}/webhooks/agentmail"
    if not url.startswith("https://"):
        sys.exit(f"PUBLIC_BASE_URL must be a public https URL AgentMail can reach, not {settings.public_base_url}")

    api = httpx.Client(
        base_url=settings.agentmail_base_url,
        headers={"Authorization": f"Bearer {settings.agentmail_api_key}"},
        timeout=30,
    )
    hooks = api.get("/webhooks").raise_for_status().json().get("webhooks", [])
    for hook in hooks:
        if hook.get("url") == url:
            print(f"Already registered: {url} ({hook.get('webhook_id')})")
            print("Its secret is in the AgentMail console if you need it again.")
            return

    hook = (
        api.post(
            "/webhooks",
            json={"url": url, "event_types": ["message.received"], "inbox_ids": [settings.agentmail_inbox_id]},
        )
        .raise_for_status()
        .json()
    )
    print(f"Registered {url} ({hook.get('webhook_id')})")

    for old in hooks:
        if (
            old.get("enabled")
            and old.get("url", "").endswith("/webhooks/agentmail")
            and settings.agentmail_inbox_id in (old.get("inbox_ids") or [])
        ):
            api.patch(f"/webhooks/{old['webhook_id']}", json={"enabled": False}).raise_for_status()
            print(f"Disabled the earlier webhook {old['webhook_id']} ({old['url']})")

    print(f"Add this to .env:\nAGENTMAIL_WEBHOOK_SECRET={hook.get('secret')}")


if __name__ == "__main__":
    main()
