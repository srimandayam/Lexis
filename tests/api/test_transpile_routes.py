"""Prove the transpile route wires to the right emitter for each target.

Transpilation *correctness* is already covered by the existing tests/test_*.py
(test_sql_emitters.py, test_cube_emitter.py, test_dbt_ossie.py, test_mcp.py) - these
tests only prove the route calls the right function with the right args.
"""

import json

import pytest


@pytest.fixture()
def model_id(client_as, tpcds_yaml) -> int:
    resp = client_as("editor").post("/api/models", json={"yaml_text": tpcds_yaml})
    return resp.json()["id"]


@pytest.mark.parametrize(
    ("target", "expected_substring"),
    [
        ("duckdb", 'FROM tpcds.public.store_sales AS "store_sales"'),
        ("postgres", 'FROM tpcds.public.store_sales AS "store_sales"'),
        ("bigquery", "FROM tpcds.public.store_sales AS `store_sales`"),
        ("databricks", "FROM tpcds.public.store_sales AS `store_sales`"),
        ("snowflake", 'FROM tpcds.public.store_sales AS "store_sales"'),
    ],
)
def test_sql_targets(client_as, model_id, target, expected_substring):
    resp = client_as("viewer").post(
        f"/api/models/{model_id}/transpile", json={"target": target, "metric": "total_sales"}
    )
    assert resp.status_code == 200
    assert expected_substring in resp.json()["content"]


def test_sql_target_without_metric_is_422(client_as, model_id):
    resp = client_as("viewer").post(f"/api/models/{model_id}/transpile", json={"target": "duckdb"})
    assert resp.status_code == 422


def test_cube_target(client_as, model_id):
    resp = client_as("viewer").post(f"/api/models/{model_id}/transpile", json={"target": "cube"})
    assert resp.status_code == 200
    assert "cubes:" in resp.json()["content"]


def test_dbt_target(client_as, model_id):
    resp = client_as("viewer").post(f"/api/models/{model_id}/transpile", json={"target": "dbt"})
    assert resp.status_code == 200
    body = resp.json()
    assert json.loads(body["content"])["version"] == "0.1.1"
    assert any("not dbt-supported" in w for w in body["warnings"])


def test_mcp_target(client_as, model_id):
    resp = client_as("viewer").post(f"/api/models/{model_id}/transpile", json={"target": "mcp"})
    assert resp.status_code == 200
    assert "tools" in json.loads(resp.json()["content"])


def test_snowflake_semantic_view_target(client_as, model_id):
    resp = client_as("viewer").post(
        f"/api/models/{model_id}/transpile", json={"target": "snowflake_semantic_view"}
    )
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert content.startswith("CREATE OR REPLACE SEMANTIC VIEW")
    assert "TABLES (" in content


def test_sml_target_returns_a_file_map(client_as, model_id):
    resp = client_as("viewer").post(f"/api/models/{model_id}/transpile", json={"target": "sml"})
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert isinstance(content, dict)
    assert "catalog.yml" in content
    assert "datasets/store_sales.yml" in content


def test_lookml_target_returns_a_file_tree(client_as, model_id):
    resp = client_as("viewer").post(
        f"/api/models/{model_id}/transpile", json={"target": "lookml"}
    )
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert isinstance(content, dict)
    assert "tpcds_retail_model.model.lkml" in content
    assert any(name.startswith("views/") for name in content)


def test_lookml_options_reach_the_emitter(client_as, model_id):
    resp = client_as("viewer").post(
        f"/api/models/{model_id}/transpile",
        json={"target": "lookml", "options": {"connection": "warehouse_a"}},
    )
    assert resp.status_code == 200
    assert 'connection: "warehouse_a"' in resp.json()["content"]["tpcds_retail_model.model.lkml"]


def test_lookml_without_options_warns_about_the_placeholder(client_as, model_id):
    resp = client_as("viewer").post(
        f"/api/models/{model_id}/transpile", json={"target": "lookml"}
    )
    assert resp.status_code == 200
    assert any("lexis_connection" in w for w in resp.json()["warnings"])


def test_unknown_option_is_422(client_as, model_id):
    """dispatch raises ValueError, which main.py's global handler maps to 422."""
    resp = client_as("viewer").post(
        f"/api/models/{model_id}/transpile",
        json={"target": "lookml", "options": {"conection": "typo"}},
    )
    assert resp.status_code == 422
    assert "does not accept option" in resp.json()["detail"]


def test_option_on_a_target_that_accepts_none_is_422(client_as, model_id):
    resp = client_as("viewer").post(
        f"/api/models/{model_id}/transpile",
        json={"target": "cube", "options": {"connection": "x"}},
    )
    assert resp.status_code == 422
