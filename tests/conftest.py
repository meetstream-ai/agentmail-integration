import pytest

import app.main


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch):
    # Retries of a failed email run immediately in tests.
    monkeypatch.setattr(app.main, "EMAIL_RETRY_DELAYS", (0, 0, 0))
