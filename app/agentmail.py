"""The parts of the AgentMail API the agent uses: read an email, reply to it."""

from urllib.parse import quote

import httpx


def _id(value: str) -> str:
    # Message ids are RFC 5322 ids like "<abc@host>"; inbox ids contain "@".
    return quote(value, safe="")


class AgentMail:
    def __init__(self, api_key: str, base_url: str, http: httpx.AsyncClient):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http

    def _message_url(self, inbox_id: str, message_id: str) -> str:
        return f"{self._base}/inboxes/{_id(inbox_id)}/messages/{_id(message_id)}"

    async def get_message(self, inbox_id: str, message_id: str) -> dict:
        resp = await self._http.get(self._message_url(inbox_id, message_id), headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    async def _download(self, url: str) -> bytes:
        # Signed CDN URL: no API key on this request.
        resp = await self._http.get(url)
        resp.raise_for_status()
        return resp.content

    async def get_attachment(self, inbox_id: str, message_id: str, attachment_id: str) -> bytes:
        url = f"{self._message_url(inbox_id, message_id)}/attachments/{_id(attachment_id)}"
        resp = await self._http.get(url, headers=self._headers)
        resp.raise_for_status()
        return await self._download(resp.json()["download_url"])

    async def get_raw(self, inbox_id: str, message_id: str) -> bytes:
        resp = await self._http.get(f"{self._message_url(inbox_id, message_id)}/raw", headers=self._headers)
        resp.raise_for_status()
        return await self._download(resp.json()["download_url"])

    async def send(self, inbox_id: str, to: list[str], subject: str, text: str, html: str | None = None) -> dict:
        body: dict = {"to": to, "subject": subject, "text": text}
        if html:
            body["html"] = html
        resp = await self._http.post(
            f"{self._base}/inboxes/{_id(inbox_id)}/messages/send", headers=self._headers, json=body
        )
        resp.raise_for_status()
        return resp.json()

    async def reply(
        self,
        inbox_id: str,
        message_id: str,
        text: str,
        html: str | None = None,
        to: list[str] | None = None,
    ) -> dict:
        body: dict = {"text": text}
        if html:
            body["html"] = html
        if to:
            body["to"] = to
        resp = await self._http.post(
            f"{self._message_url(inbox_id, message_id)}/reply", headers=self._headers, json=body
        )
        resp.raise_for_status()
        return resp.json()
