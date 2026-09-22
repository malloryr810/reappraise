import pytest


@pytest.fixture(autouse=True)
def _clear_ebay_browse_api_credentials(monkeypatch):
    """Remove Browse API credentials for every test.

    test_app.py imports app.py at module level, which calls load_dotenv() and
    sets EBAY_CLIENT_ID / EBAY_CLIENT_SECRET for the entire pytest process.
    Without this, any test that doesn't patch httpx would call the real eBay API
    with those credentials. Clearing them sends such tests to the mock fallback
    instead, regardless of test ordering.

    Tests that need Browse API credentials can override via monkeypatch.setenv().
    """
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
