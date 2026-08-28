import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_sidebar_active_navigation_state_across_routes(client: TestClient):
    """
    Verifies that the sidebar navigation active-state logic accurately reflects
    the current route/page across the operational sourcing modules without defaulting or lagging,
    and verifies that Settings is accessed via the user profile card.
    """
    operational_routes = [
        ("/", 'href="/" class="sidebar-link active"'),
        ("/rfqs", 'href="/rfqs" class="sidebar-link active"'),
        ("/rfqs/create", 'href="/rfqs" class="sidebar-link active"'),
        ("/item-master", 'href="/item-master" class="sidebar-link active"'),
        ("/item-master/import", 'href="/item-master" class="sidebar-link active"'),
        ("/review", 'href="/review" class="sidebar-link active"'),
        ("/comparisons", 'href="/comparisons" class="sidebar-link active"'),
        ("/comparisons/create", 'href="/comparisons" class="sidebar-link active"'),
        ("/analytics", 'href="/analytics" class="sidebar-link active"'),
    ]

    for path, expected_active_snippet in operational_routes:
        res = client.get(path)
        assert res.status_code == 200, f"Failed to load {path}"
        assert expected_active_snippet in res.text, (
            f"Active navigation highlight mismatch for route '{path}'. "
            f"Expected snippet '{expected_active_snippet}' in HTML."
        )

        # Verify that only ONE operational sidebar item is marked active for that page
        active_count = res.text.count('class="sidebar-link active"')
        assert active_count == 1, f"Route '{path}' rendered {active_count} active sidebar links, expected exactly 1."

    # Verify /settings is directly accessible and linked via the Aarav Unit profile card
    res_settings = client.get("/settings")
    assert res_settings.status_code == 200, "Failed to load /settings"
    assert 'class="user-profile-card"' in res_settings.text
    assert 'href="/settings"' in res_settings.text
    # On /settings, operational sourcing links have no active highlight
    active_count_settings = res_settings.text.count('class="sidebar-link active"')
    assert active_count_settings == 0, f"Expected 0 active operational sidebar links on /settings, got {active_count_settings}."
