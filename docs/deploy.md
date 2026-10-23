# Deploy the reference app

The app in [`app/`](../app/) is one small web service. To run it for a team, it needs:

- **A public HTTPS address.** AgentMail and MeetStream send it webhooks.
- **One instance with a persistent disk.** It keeps its meetings in SQLite, so run a
  single copy and put the database on a volume that survives redeploys.
- **Its settings as environment variables:** the same ones as [`.env.example`](../.env.example).

The [Dockerfile](../Dockerfile) packages all of this. It stores the database at
`/data/meetings.db`, serves on `$PORT` (8000 if unset), and answers `GET /healthz` for
health checks.

## 1. Try the container locally

```bash
cp .env.example .env          # fill it in, as in the README
docker compose up -d --build
curl http://localhost:8000/healthz      # {"ok":true}
```

The database is kept in the `notetaker-data` volume. Logs: `docker compose logs -f`.
Inside a container, your browser counts as a remote client, so the calendar pages need
`ADMIN_TOKEN` there too (step 3).

## 2. Pick a host

Any host that runs a Docker image with a persistent volume works. Some examples:

| Host | Set up |
|---|---|
| [Render](https://render.com) | New **Web Service** from this repo (it finds the Dockerfile). Add a **Disk** mounted at `/data`. Health check path `/healthz`. |
| [Railway](https://railway.com) | New project from this repo. Add a **Volume** mounted at `/data`. |
| [Fly.io](https://fly.io) | `fly launch` (keep the Dockerfile), then `fly volumes create data` and mount it at `/data`. Keep one machine: `fly scale count 1`. |
| A VM (any cloud) | Install Docker, clone the repo, `docker compose up -d`, and put [Caddy](https://caddyserver.com) or nginx in front for HTTPS. |

Whichever you use, keep it to **one instance**, and don't let it scale to zero: it checks
on meetings every two minutes and schedules the next meeting of each recurring series.

## 3. Set the environment

Copy every value from your `.env` into the host's environment settings, then change these
for the hosted address (say `https://notes.example.com`):

| Variable | Value |
|---|---|
| `PUBLIC_BASE_URL` | `https://notes.example.com` |
| `ADMIN_TOKEN` | A long random string, e.g. from `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `GOOGLE_REDIRECT_URI` | `https://notes.example.com/calendar/callback` (if you connect Google calendars) |
| `MICROSOFT_REDIRECT_URI` | `https://notes.example.com/calendar/outlook/callback` (if you connect Outlook calendars) |

Add the same redirect URIs to your Google OAuth client and your Microsoft Entra app.

## 4. Point AgentMail at it

Run this once with the hosted `PUBLIC_BASE_URL`, from your machine or the host's shell:

```bash
python scripts/register_webhook.py
# or, inside the container:
docker compose run --rm notetaker python scripts/register_webhook.py
```

It prints `AGENTMAIL_WEBHOOK_SECRET`. Add that to the host's environment and restart.
Earlier webhooks for the inbox (say, a tunnel's) are turned off.

## 5. Check it

1. `https://notes.example.com/healthz` answers `{"ok":true}`.
2. Email the agent's inbox an invite from an address in `ALLOWED_SENDERS`. It replies
   within a few seconds.
3. To connect calendars, sign in once at `https://notes.example.com/calendar/login` with
   `ADMIN_TOKEN`, then open `/calendar`.

## Keeping it running

- **Updates:** redeploy the new image. The database and its schema carry over; new
  columns are added on start.
- **Backups:** copy `/data/meetings.db` (it's one file) on whatever schedule your host offers.
- **Secrets:** keep keys in the host's secret store, never in the image. The image never
  contains `.env`.
- **Logs:** the app logs each invite, bot and notes email, never transcripts or keys.
