import pytest

from qq_codex_bot import ingress


@pytest.fixture(autouse=True)
def isolate_ingress_cache():
    """Each test creates its own bridge; its in-memory inbox must be isolated too."""
    ingress._RECENT.clear()
    ingress._IMAGE_FOLLOWUPS.clear()
    yield
    ingress._RECENT.clear()
    ingress._IMAGE_FOLLOWUPS.clear()
