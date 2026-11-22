# Contributing

Thanks for helping. Bug reports, fixes and new integrations are all welcome.

## Set up

```bash
git clone https://github.com/meetstream-ai/agentmail-integration.git
cd agentmail-integration
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt "./toolkit[dev]"
```

## Before you open a pull request

```bash
ruff check . && ruff format --check .
mypy app scripts --ignore-missing-imports
pytest                       # the app
cd toolkit && pytest         # the toolkit
```

CI runs the same checks, plus the Docker build and the package build.

- **Add a test** for any change in behaviour. Tests mock AgentMail, MeetStream and the
  models, so they never need keys.
- **Keep the app and toolkit in step.** The invite parser in `toolkit/` is shared with
  `app/`, so a change there affects both.
- **Update the docs** if setup or behaviour changes, and add a line to `CHANGELOG.md`.
- **API behaviour**: [AGENTS.md](AGENTS.md) lists the AgentMail and MeetStream details
  that are easy to get wrong. Check the live docs before changing how either API is called.

## Reporting a bug

Open an issue with what you did, what you expected and what happened, plus the log lines
around it. Leave out keys, transcripts and other people's addresses. For security issues,
see [SECURITY.md](SECURITY.md) instead.
