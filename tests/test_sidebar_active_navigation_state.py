import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_sidebar_active_navigation_state_across_routes(client: TestClient):
    """
    Verifies that the sidebar navigation active-state logic accurately reflects
    the current route/page across the entire application without defaulting or lagging.
    """
    routes_to_test = [
        ("/", 'href="/" class="sidebar-link active"'),
        ("/rfqs", 'href="/rfqs" class="sidebar-link active"'),
        ("/rfqs/create", 'href="/rfqs" class="sidebar-link active"'),
        ("/item-master", 'href="/item-master" class="sidebar-link active"'),
        ("/item-master/import", 'href="/item-master" class="sidebar-link active"'),
        ("/review", 'href="/review" class="sidebar-link active"'),
        ("/comparisons", 'href="/comparisons" class="sidebar-link active"'),
        ("/comparisons/create", 'href="/comparisons" class="sidebar-link active"'),
        ("/analytics", 'href="/analytics" class="sidebar-link active"'),
        ("/settings", 'href="/settings" class="sidebar-link active"'),
    ]

    for path, expected_active_snippet in routes_to_test:
        res = client.get(path)
        assert res.status_code == 200, f"Failed to load {path}"
        assert expected_active_snippet in res.text, (
            f"Active navigation highlight mismatch for route '{path}'. "
            f"Expected snippet '{expected_active_snippet}' in HTML."
        )

        # Verify that only ONE sidebar item is marked active for that page
        active_count = res.text.count('class="sidebar-link active"')
        assert active_count == 1, f"Route '{path}' rendered {active_count} active sidebar links, expected exactly 1."
