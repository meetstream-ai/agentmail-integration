import asyncio
import html
import json
import logging
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from hmac import compare_digest
from urllib.parse import parse_qs, urlencode

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from svix.webhooks import Webhook, WebhookVerificationError

from app import calendar
from app.agentmail import AgentMail
from app.config import Settings, get_settings
from app.flows import Agent
from app.meetstream import MeetStream
from app.notes import make_note_writer
from app.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("notetaker")
# httpx logs every URL at INFO, including signed attachment download links.
logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run(job, *args) -> None:
    try:
        await job(*args)
    except Exception:
        log.exception("%s failed", job.__name__)


# Waits before each retry of an email that failed to process (seconds).
EMAIL_RETRY_DELAYS = (2, 10, 30)
OAUTH_STATE_TTL = 600  # seconds a sign-in attempt stays valid
ADMIN_COOKIE = "notetaker_admin"


async def _process_email(agent: Agent, event_id: str, inbox_id: str, message_id: str) -> None:
    """Handle one inbound email. AgentMail already has its 200, so it won't
    resend: retry here, and if every try fails, forget the delivery so a
    redelivery or replay of the same event is handled rather than skipped."""
    for attempt, delay in enumerate((0, *EMAIL_RETRY_DELAYS), start=1):
        await asyncio.sleep(delay)
        try:
            await agent.handle_email(inbox_id, message_id)
            return
        except Exception:
            log.exception("handling %s failed (try %d)", message_id, attempt)
    agent.store.forget_event(event_id)


def _calendar_redirect(**query: str) -> RedirectResponse:
    return RedirectResponse("/calendar?" + urlencode(query))


def create_app(
    settings: Settings | None = None,
    http: httpx.AsyncClient | None = None,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = http or httpx.AsyncClient(timeout=30)
        app.state.http = client
        app.state.oauth_states = {}  # state -> when it was issued
        app.state.agent = Agent(
            settings,
            Store(settings.database_path, **({"now": now} if now else {})),
            AgentMail(settings.agentmail_api_key, settings.agentmail_base_url, client),
            MeetStream(settings.meetstream_api_key, settings.meetstream_base_url, client),
            make_note_writer(settings, client),
            **({"now": now} if now else {}),
        )
        if not settings.allowed_senders.strip():
            log.warning("ALLOWED_SENDERS is empty, so every invite will be ignored")

        async def watch():
            while True:
                await asyncio.sleep(settings.poll_seconds)
                await _run(app.state.agent.sync_calendar)
                await _run(app.state.agent.sync_series)
                await _run(app.state.agent.check_unfinished)

        watcher = asyncio.create_task(watch()) if settings.poll_seconds > 0 else None
        yield
        if watcher:
            watcher.cancel()
        if http is None:
            await client.aclose()

    app = FastAPI(title="AgentMail + MeetStream notetaker", lifespan=lifespan)

    def is_local(request: Request) -> bool:
        # The connection must come from this machine: the Host header alone is
        # whatever the client says. Requests relayed by a tunnel or proxy also
        # come from this machine, so they're told apart by their forwarding
        # headers and public hostname.
        from_here = request.client is not None and request.client.host in ("127.0.0.1", "::1")
        forwarded = any(h in request.headers for h in ("cf-connecting-ip", "x-forwarded-for", "forwarded"))
        return from_here and not forwarded and request.url.hostname in ("localhost", "127.0.0.1")

    def is_admin(request: Request) -> bool:
        token = settings.admin_token
        return bool(token) and compare_digest(request.cookies.get(ADMIN_COOKIE, ""), token)

    def local_only(request: Request) -> None:
        # Connecting a calendar spends this MeetStream account's minutes on
        # that calendar's meetings, so only the person at this machine may do
        # it, or, on a hosted deployment, whoever holds ADMIN_TOKEN.
        if not (is_local(request) or is_admin(request)):
            raise HTTPException(404)

    def new_oauth_state(request: Request) -> str:
        states = request.app.state.oauth_states
        now = time.monotonic()
        for old in [s for s, at in states.items() if now - at > OAUTH_STATE_TTL]:
            del states[old]
        state = secrets.token_urlsafe(24)
        states[state] = now
        return state

    def take_oauth_state(request: Request, state: str) -> None:
        issued = request.app.state.oauth_states.pop(state, None)
        if issued is None or time.monotonic() - issued > OAUTH_STATE_TTL:
            raise HTTPException(400, "Unknown or expired sign-in attempt; start again from /calendar")

    @app.get("/calendar/login", response_class=HTMLResponse)
    async def login_page():
        # Hosted deployments only. The token is posted, not put in the URL,
        # so it never lands in an access log or the browser's history.
        if not settings.admin_token:
            raise HTTPException(404)
        return """<!doctype html><meta charset="utf-8"><title>Sign in · AgentMail Notetaker</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:420px;margin:64px auto;padding:0 16px}
input,button{font:inherit;padding:8px 10px}</style>
<h1>AgentMail Notetaker</h1>
<form method="post" action="/calendar/login"><input type="password" name="token" placeholder="Admin token" autofocus>
<button>Sign in</button></form>"""

    @app.post("/calendar/login")
    async def login(request: Request):
        if not settings.admin_token:
            raise HTTPException(404)
        token = parse_qs((await request.body()).decode("utf-8", "replace")).get("token", [""])[0]
        if not compare_digest(token, settings.admin_token):
            raise HTTPException(403, "Wrong token")
        back = RedirectResponse("/calendar", status_code=303)
        back.set_cookie(ADMIN_COOKIE, token, httponly=True, secure=request.url.scheme == "https", samesite="lax")
        return back

    @app.get("/calendar", response_class=HTMLResponse)
    async def calendar_page(request: Request, connected: str = "", error: str = ""):
        local_only(request)
        accounts = sorted(set(request.app.state.agent.store.calendars().values()))
        rows = "".join(f"<li>{html.escape(a)}</li>" for a in accounts) or "<li>None yet</li>"
        notice = (
            f'<p class="ok">Connected {html.escape(connected)}.</p>'
            if connected
            else f'<p class="err">{html.escape(error)}</p>'
            if error
            else ""
        )
        buttons = []
        if settings.google_client_id and settings.google_client_secret:
            buttons.append('<a class="btn" href="/calendar/connect">Connect Google Calendar</a>')
        if settings.microsoft_client_id and settings.microsoft_client_secret:
            buttons.append('<a class="btn" href="/calendar/outlook/connect">Connect Outlook Calendar</a>')
        button = " ".join(buttons) or (
            "<p>Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET or MICROSOFT_CLIENT_ID / "
            "MICROSOFT_CLIENT_SECRET in .env first.</p>"
        )
        return f"""<!doctype html><meta charset="utf-8"><title>Calendar · AgentMail Notetaker</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#1a1a1a}}
.btn{{display:inline-block;background:#1a73e8;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none}}
.ok{{color:#137333}}.err{{color:#c5221f}}small{{color:#666}}</style>
<h1>AgentMail Notetaker</h1>
<p>Connect a Google or Outlook calendar and the notetaker joins every meeting on it tagged <b>{html.escape(settings.calendar_tag)}</b> that has a Zoom, Google Meet or Teams
link, then emails the notes to the attendees from <b>{html.escape(settings.agentmail_inbox_id)}</b>.</p>
{notice}<h3>Connected calendars</h3><ul>{rows}</ul>{button}
<p><small>Read-only access. The calendar is synced by MeetStream; nothing on it is ever changed.</small></p>"""

    @app.get("/calendar/connect")
    async def calendar_connect(request: Request):
        local_only(request)
        state = new_oauth_state(request)
        return RedirectResponse(calendar.auth_url(settings.google_client_id, settings.google_redirect_uri, state))

    @app.get("/calendar/callback")
    async def calendar_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        local_only(request)
        take_oauth_state(request, state)
        if error or not code:
            return _calendar_redirect(error=f"Google sign-in was cancelled ({error or 'no code'})")
        agent: Agent = request.app.state.agent
        try:
            token = await calendar.refresh_token_for(
                request.app.state.http,
                settings.google_client_id,
                settings.google_client_secret,
                code,
                settings.google_redirect_uri,
            )
            connection = await agent.meetstream.connect_calendar(
                settings.google_client_id, settings.google_client_secret, token
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.exception("connecting a calendar failed")
            return _calendar_redirect(error=str(exc)[:300])
        return await _connected(agent, connection, "google")

    @app.get("/calendar/outlook/connect")
    async def outlook_connect(request: Request):
        local_only(request)
        state = new_oauth_state(request)
        return RedirectResponse(
            calendar.microsoft_auth_url(
                settings.microsoft_client_id, settings.microsoft_tenant_id, settings.microsoft_redirect_uri, state
            )
        )

    @app.get("/calendar/outlook/callback")
    async def outlook_callback(
        request: Request, state: str = "", code: str = "", error: str = "", error_description: str = ""
    ):
        local_only(request)
        take_oauth_state(request, state)
        if error or not code:
            return _calendar_redirect(
                error=f"Microsoft sign-in failed: {(error_description or error or 'no code')[:200]}"
            )
        agent: Agent = request.app.state.agent
        try:
            token = await calendar.microsoft_refresh_token_for(
                request.app.state.http,
                settings.microsoft_client_id,
                settings.microsoft_client_secret,
                settings.microsoft_tenant_id,
                code,
                settings.microsoft_redirect_uri,
            )
            connection = await agent.meetstream.connect_outlook_calendar(
                settings.microsoft_client_id, settings.microsoft_client_secret, token, settings.microsoft_tenant_id
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.exception("connecting an Outlook calendar failed")
            return _calendar_redirect(error=str(exc)[:300])
        return await _connected(agent, connection, "outlook")

    def _calendar_ids(record: dict) -> set:
        """Every id MeetStream may use to name this connection's calendars in its events."""
        ids = {
            record.get(k) for k in ("calendar_id", "primary_calendar_id", "default_calendar_id", "account_id", "email")
        }
        ids |= {c.get("id") for c in record.get("calendars") or [] if isinstance(c, dict)}
        return {i for i in ids if isinstance(i, str) and i}

    async def _connected(agent: Agent, connection: dict, provider: str) -> RedirectResponse:
        account = connection.get("user_email") or connection.get("account_id") or connection.get("email")
        ids = _calendar_ids(connection)
        if not ids or not account:
            # MeetStream's API reference shows an empty connect response for
            # Outlook; its guide says the ids are there. Either way, its list
            # of connections has them.
            try:
                records = [c for c in await agent.meetstream.calendar_connections() if c.get("provider") == provider]
            except httpx.HTTPError:
                records = []
            if account:
                records = [c for c in records if account in (c.get("account_id"), c.get("email"))]
            if len(records) != 1:
                log.warning("couldn't tell which %s connection was just made: %d candidates", provider, len(records))
                return _calendar_redirect(
                    error="Connected, but MeetStream didn't say which calendar. Connect it again."
                )
            account = account or records[0].get("email") or records[0].get("account_id") or provider
            ids |= _calendar_ids(records[0])
        for calendar_id in ids:
            agent.store.add_calendar(calendar_id, account)
        await _run(agent.sync_calendar)
        return _calendar_redirect(connected=account)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/webhooks/agentmail")
    async def agentmail_webhook(request: Request, tasks: BackgroundTasks):
        if not settings.agentmail_webhook_secret:
            raise HTTPException(503, "AGENTMAIL_WEBHOOK_SECRET is not set")
        body = await request.body()
        try:
            # Signed over the raw body, so verify before parsing it.
            Webhook(settings.agentmail_webhook_secret).verify(body, dict(request.headers))
        except WebhookVerificationError:
            raise HTTPException(400, "invalid signature") from None
        payload = json.loads(body)

        agent: Agent = request.app.state.agent
        message = payload.get("message") or {}
        if payload.get("event_type") != "message.received" or message.get("inbox_id") != settings.agentmail_inbox_id:
            return {"ok": True}
        event_id = payload.get("event_id") or request.headers["svix-id"]
        if not agent.store.first_time_seen(event_id):
            return {"ok": True}
        # Answer fast; AgentMail retries anything slow.
        tasks.add_task(_process_email, agent, event_id, message["inbox_id"], message["message_id"])
        return {"ok": True}

    @app.post("/webhooks/meetstream/{token}")
    async def meetstream_webhook(token: str, request: Request, tasks: BackgroundTasks):
        agent: Agent = request.app.state.agent
        if agent.store.by_token(token) is None:
            raise HTTPException(404)
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "body is not JSON") from None
        tasks.add_task(_run, agent.handle_meetstream, token, payload)
        return {"ok": True}

    return app
