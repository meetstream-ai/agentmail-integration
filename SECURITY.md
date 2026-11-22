# Security

## Reporting a vulnerability

Please report security issues privately, not in a public issue: email
**hello@meetstream.ai** with "Security" in the subject, or use GitHub's
**Report a vulnerability** button on this repository's **Security** tab.

Include what you found, how to reproduce it, and what it affects. We'll acknowledge the
report, keep you updated while we fix it, and credit you if you'd like.

## Running it safely

- Keep keys in the environment or your host's secret store, never in the repository or
  image. `.env` is git-ignored and left out of the Docker image.
- Set `ALLOWED_SENDERS` to the people and domains you trust. Empty means nobody: every bot
  records other people and spends MeetStream minutes, so the inbox never answers strangers.
- On a hosted deployment, set `ADMIN_TOKEN`; without it the calendar pages answer only the
  machine the server runs on.
- AgentMail webhooks are verified with their signing secret, and every MeetStream callback
  URL carries its own random token. Don't put either in logs or screenshots.
