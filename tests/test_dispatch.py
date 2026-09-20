"""Tests for lexis.dispatch's target-alias resolution and per-target options."""

import pytest

from lexis.dispatch import TARGET_ALIASES, transpile


def test_ssv_is_an_alias_for_snowflake_semantic_view(tpcds_document, tpcds_model):
    assert TARGET_ALIASES["ssv"] == "snowflake_semantic_view"

    aliased = transpile(tpcds_document, tpcds_model, "ssv")
    canonical = transpile(tpcds_document, tpcds_model, "snowflake_semantic_view")
    assert aliased == canonical


def test_lookml_is_a_multi_file_target(tpcds_document, tpcds_model):
    result = transpile(tpcds_document, tpcds_model, "lookml")
    assert isinstance(result.content, dict)
    assert "tpcds_retail_model.model.lkml" in result.content
    assert any(name.startswith("views/") for name in result.content)


def test_lookml_options_reach_the_emitter(tpcds_document, tpcds_model):
    result = transpile(
        tpcds_document, tpcds_model, "lookml", options={"connection": "warehouse_a"}
    )
    assert 'connection: "warehouse_a"' in result.content["tpcds_retail_model.model.lkml"]


def test_lookml_dialect_option_is_coerced_and_validated(tpcds_document, tpcds_model):
    result = transpile(tpcds_document, tpcds_model, "lookml", options={"dialect": "bigquery"})
    assert "CONCAT(" in result.content["views/store_sales.view.lkml"]

    with pytest.raises(ValueError, match="unknown lookml dialect"):
        transpile(tpcds_document, tpcds_model, "lookml", options={"dialect": "klingon"})


def test_unknown_option_is_rejected_rather_than_ignored(tpcds_document, tpcds_model):
    """A typo'd option should surface immediately, not silently do nothing."""
    with pytest.raises(ValueError, match="does not accept option"):
        transpile(tpcds_document, tpcds_model, "lookml", options={"conection": "x"})


def test_options_on_a_target_that_accepts_none_is_rejected(tpcds_document, tpcds_model):
    with pytest.raises(ValueError, match="accepted: none"):
        transpile(tpcds_document, tpcds_model, "cube", options={"connection": "x"})
