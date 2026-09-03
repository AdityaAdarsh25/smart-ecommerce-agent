"""Catalogue search filtering."""


def search(client, **params):
    response = client.get("/app/v1/search", params=params)
    assert response.status_code == 200
    return {p["product_name"]: p["cost"] for p in response.json()}


# --- 14. max_price filter -------------------------------------------------


def test_max_price_filters_by_cost(client, seed):
    """Previously `query.filter(product_db.cost)` -- a truthy column
    expression that silently returned everything."""
    results = search(client, item="Test", max_price=300)
    assert set(results) == {"Test Widget", "Test Gadget"}
    assert all(cost <= 300 for cost in results.values())


def test_max_price_boundary_is_inclusive(client, seed):
    assert set(search(client, item="Test", max_price=100)) == {"Test Widget"}


def test_max_price_excluding_everything_returns_empty(client, seed):
    assert search(client, item="Test", max_price=1) == {}


def test_without_max_price_all_matches_returned(client, seed):
    assert len(search(client, item="Test")) == 3


def test_brand_and_max_price_combine(client, seed):
    assert set(search(client, item="Test", brand="Acme", max_price=150)) == {
        "Test Widget"
    }


def test_missing_item_is_rejected(client, seed):
    assert client.get("/app/v1/search").status_code == 400
