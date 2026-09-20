"""Tests for the Ossie -> LookML emitter.

Every emitted file is round-tripped through `lkml` - an independent third-party
LookML parser - so a malformed block is a test failure rather than something a
Looker developer discovers later. Structural assertions then run against the
parsed dicts, the same way test_cube_emitter.py asserts against `yaml.safe_load`
output rather than raw text.
"""

from pathlib import Path

import lkml
import pytest

from lexis._vendor.ossie import OssieDialect
from lexis.sml._common import decompose_simple_aggregate
from lexis.transpilers.lookml import DEFAULT_CONNECTION, emit_lookml_project
from lexis.transpilers.lookml.naming import NameRegistry, slugify
from lexis.transpilers.lookml.writer import Bare, Block, Sql, render, render_value

# Matches tests/conftest.py rather than importing it - the other test modules
# that need a fixture path resolve it the same way.
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def emitted(tpcds_model):
    return emit_lookml_project(tpcds_model, connection="tpcds_warehouse")


@pytest.fixture()
def parsed(emitted):
    return {name: lkml.load(content) for name, content in emitted.files.items()}


def _view(parsed, name):
    return next(v for v in parsed[f"views/{name}.view.lkml"]["views"] if v["name"] == name)


def _fields(view, kind):
    return {f["name"]: f for f in view.get(kind, [])}


# --- structure -------------------------------------------------------------


def test_every_emitted_file_is_parseable_lookml(emitted):
    assert emitted.files, "emitter produced no files"
    for name, content in emitted.files.items():
        lkml.load(content)  # raises on malformed LookML


def test_emits_one_view_per_dataset_plus_a_model_file(emitted, tpcds_model):
    expected = {f"views/{name}.view.lkml" for name in tpcds_model.datasets}
    expected.add("tpcds_retail_model.model.lkml")
    assert set(emitted.files) == expected


def test_views_carry_their_source_as_sql_table_name(parsed, tpcds_model):
    for name, dataset in tpcds_model.datasets.items():
        assert _view(parsed, name)["sql_table_name"] == dataset.source


def test_model_file_declares_connection_and_include(parsed):
    model = parsed["tpcds_retail_model.model.lkml"]
    assert model["connection"] == "tpcds_warehouse"
    assert model["includes"] == ["/views/*.view.lkml"]


# --- explores --------------------------------------------------------------


def test_explore_is_rooted_at_the_fact_table_and_joins_every_dimension(parsed):
    explores = parsed["tpcds_retail_model.model.lkml"]["explores"]
    assert [e["name"] for e in explores] == ["store_sales"]
    joins = explores[0]["joins"]
    assert {j["name"] for j in joins} == {"date_dim", "customer", "item", "store"}
    assert all(j["relationship"] == "many_to_one" for j in joins)
    assert all(j["type"] == "left_outer" for j in joins)


def test_join_conditions_reference_real_fields_on_both_sides(parsed):
    joins = {j["name"]: j for j in parsed["tpcds_retail_model.model.lkml"]["explores"][0]["joins"]}
    assert joins["date_dim"]["sql_on"].strip() == "${store_sales.ss_sold_date_sk} = ${date_dim.d_date_sk}"

    store_sales = _view(parsed, "store_sales")
    assert "ss_sold_date_sk" in _fields(store_sales, "dimensions")
    assert "d_date_sk" in _fields(_view(parsed, "date_dim"), "dimensions")


# --- dimensions ------------------------------------------------------------


def test_composite_primary_key_is_synthesized_as_a_concatenation(emitted, parsed):
    key = _fields(_view(parsed, "store_sales"), "dimensions")["lexis_primary_key"]
    assert key["primary_key"] == "yes"
    assert key["hidden"] == "yes"
    assert "ss_item_sk" in key["sql"] and "ss_ticket_number" in key["sql"]
    assert any("composite primary key" in w for w in emitted.warnings)


def test_single_column_primary_key_marks_that_dimension(parsed):
    assert _fields(_view(parsed, "date_dim"), "dimensions")["d_date_sk"]["primary_key"] == "yes"


def test_undeclared_key_column_gets_a_hidden_dimension(parsed):
    """`ss_ticket_number` is in the fixture's primary_key but is not an Ossie field."""
    ticket = _fields(_view(parsed, "store_sales"), "dimensions")["ss_ticket_number"]
    assert ticket["hidden"] == "yes"
    assert ticket["sql"] == "${TABLE}.ss_ticket_number"


def test_temporal_field_becomes_a_dimension_group_named_without_its_suffix(parsed):
    """Looker re-appends the timeframe, so `d_date` must become group `d` to keep
    its reference name `${date_dim.d_date}` rather than `d_date_date`."""
    groups = _fields(_view(parsed, "date_dim"), "dimension_groups")
    assert set(groups) == {"d"}
    assert groups["d"]["type"] == "time"
    assert "date" in groups["d"]["timeframes"]
    assert groups["d"]["sql"] == "${TABLE}.d_date"


def test_is_time_without_a_temporal_datatype_stays_a_plain_dimension(emitted, parsed):
    """The fixture marks `d_year`/`d_month_name` is_time although they are an integer
    and a string; a LookML `type: time` group over either emits invalid SQL."""
    date_dim = _view(parsed, "date_dim")
    assert {"d_year", "d_quarter_name", "d_month_name"} <= set(_fields(date_dim, "dimensions"))
    assert set(_fields(date_dim, "dimension_groups")) == {"d"}
    for name in ("d_year", "d_quarter_name", "d_month_name"):
        assert any(f"field {name!r} is marked dimension.is_time" in w for w in emitted.warnings)


def test_ai_context_synonyms_become_tags(parsed):
    dimension = _fields(_view(parsed, "store_sales"), "dimensions")["ss_sold_date_sk"]
    assert dimension["tags"] == ["sale date", "transaction date"]


def test_multi_column_expression_is_passed_through_and_warned_about(emitted, parsed):
    full_name = _fields(_view(parsed, "customer"), "dimensions")["customer_full_name"]
    assert full_name["sql"] == "c_first_name || ' ' || c_last_name"
    assert any("is not a bare column" in w for w in emitted.warnings)


# --- measures --------------------------------------------------------------


def test_simple_aggregate_metrics_become_typed_measures(parsed, tpcds_model):
    """Cross-check every measure against the shared decomposer, so the LookML
    mapping can't drift from the one SML uses."""
    measures = _fields(_view(parsed, "store_sales"), "measures")
    for metric_name, metric in tpcds_model.metrics.items():
        expression = tpcds_model.resolve_expression(metric.expression, OssieDialect.ANSI_SQL)
        decomposed = decompose_simple_aggregate(expression)
        if decomposed is None:
            continue
        method, _, column = decomposed
        assert measures[metric_name]["type"] == {"sum": "sum"}[method]
        assert measures[metric_name]["sql"] == "${TABLE}." + column


def test_ratio_metric_reuses_the_first_declared_matching_measure(parsed):
    """`total_sales` and `sales_by_brand` are both SUM(store_sales.ss_ext_sales_price);
    the ratio must reuse the one declared first, not whichever was seen last."""
    measures = _fields(_view(parsed, "store_sales"), "measures")
    clv = measures["customer_lifetime_value"]
    assert clv["type"] == "number"
    assert clv["sql"].startswith("${store_sales.total_sales} /")


def test_nullif_guard_is_preserved_on_a_ratio_denominator(parsed):
    productivity = _fields(_view(parsed, "store_sales"), "measures")["store_productivity"]
    assert "NULLIF(" in productivity["sql"]


def test_ratio_component_without_an_existing_measure_is_synthesized_hidden(parsed):
    """COUNT(DISTINCT customer.c_customer_sk) has no metric of its own, so the
    emitter adds a hidden count_distinct measure on the customer view."""
    denominator = _fields(_view(parsed, "customer"), "measures")["customer_lifetime_value_denominator"]
    assert denominator["type"] == "count_distinct"
    assert denominator["hidden"] == "yes"
    assert denominator["sql"] == "${TABLE}.c_customer_sk"


def test_cross_view_ratio_warns_about_reach_not_correctness(emitted):
    """A ratio divides two typed measure references, so symmetric aggregates still
    apply - the caveat is that it only resolves where both views are joined."""
    warning = next(w for w in emitted.warnings if "customer_lifetime_value" in w)
    assert "divides measures across views" in warning
    assert "symmetric aggregates still apply" in warning


def test_raw_cross_dataset_metric_warns_that_symmetric_aggregates_do_not_apply(tpcds_model):
    """A metric that decomposes to neither a plain aggregate nor a ratio falls back
    to raw SQL in a `type: number` measure, which Looker does NOT protect."""
    from lexis._vendor.ossie import (
        OssieDialectExpression,
        OssieExpression,
        OssieMetric,
        OssieSemanticModel,
    )
    from lexis.resolved_model import ResolvedModel

    raw = OssieMetric(
        name="blended_margin",
        expression=OssieExpression(
            dialects=[
                OssieDialectExpression(
                    dialect=OssieDialect.ANSI_SQL,
                    expression=(
                        "SUM(store_sales.ss_net_profit) - SUM(store_sales.ss_ext_sales_price)"
                        " + AVG(item.i_current_price)"
                    ),
                )
            ]
        ),
    )
    source = tpcds_model.semantic_model
    model = ResolvedModel.build(
        OssieSemanticModel(
            name=source.name,
            datasets=source.datasets,
            relationships=source.relationships,
            metrics=[raw],
        )
    )
    result = emit_lookml_project(model, connection="c")
    warning = next(w for w in result.warnings if "blended_margin" in w)
    assert "symmetric aggregates" in warning

    measure = _fields(
        next(v for v in lkml.load(result.files["views/store_sales.view.lkml"])["views"]),
        "measures",
    )["blended_margin"]
    assert measure["type"] == "number"
    assert "${store_sales.ss_net_profit}" in measure["sql"]
    assert "${item.i_current_price}" in measure["sql"]


# --- warnings --------------------------------------------------------------


def test_placeholder_connection_warns(tpcds_model):
    """Ossie models no connection, so an unset one is a placeholder the project
    cannot run with - it has to be loud rather than silently emitted."""
    result = emit_lookml_project(tpcds_model)
    model_file = result.files["tpcds_retail_model.model.lkml"]
    assert f'connection: "{DEFAULT_CONNECTION}"' in model_file
    assert any(DEFAULT_CONNECTION in w for w in result.warnings)


def test_named_connection_does_not_warn(emitted):
    assert not any(DEFAULT_CONNECTION in w for w in emitted.warnings)


def test_unique_keys_and_custom_extensions_are_reported_as_lossy(emitted):
    assert any(w.startswith("LOSSY:") and "unique_keys" in w for w in emitted.warnings)
    assert any(w.startswith("LOSSY:") and "custom_extensions" in w for w in emitted.warnings)


# --- dialect ---------------------------------------------------------------


def test_bigquery_dialect_uses_concat_for_a_composite_key(tpcds_model):
    result = emit_lookml_project(tpcds_model, dialect=OssieDialect.BIGQUERY)
    key = _fields(
        next(v for v in lkml.load(result.files["views/store_sales.view.lkml"])["views"]),
        "dimensions",
    )["lexis_primary_key"]
    assert key["sql"].startswith("CONCAT(")


def test_default_dialect_uses_pipe_concatenation(parsed):
    key = _fields(_view(parsed, "store_sales"), "dimensions")["lexis_primary_key"]
    assert "||" in key["sql"]


# --- writer / naming units -------------------------------------------------


def test_writer_escapes_quotes_and_collapses_newlines():
    assert render_value("label", 'a "quoted"\nvalue') == 'label: "a \\"quoted\\" value"'


def test_writer_rejects_a_sql_value_that_would_terminate_the_block():
    with pytest.raises(ValueError, match="';;'"):
        render_value("sql", Sql("SELECT 1 ;; DROP"))


def test_writer_renders_nested_blocks():
    block = Block("view", "v", [("sql_table_name", Sql("a.b"))])
    block.children.append(Block("dimension", "d", [("type", Bare("string"))]))
    assert render(block) == (
        "view: v {\n"
        "  sql_table_name: a.b ;;\n"
        "\n"
        "  dimension: d {\n"
        "    type: string\n"
        "  }\n"
        "}"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Store Sales", "store_sales"), ("2nd table", "_2nd_table"), ("  ", "unnamed"), ("A-B.C", "a_b_c")],
)
def test_slugify(name, expected):
    assert slugify(name) == expected


def test_name_collisions_are_suffixed_not_overwritten():
    warnings: list[str] = []
    registry = NameRegistry(warnings)
    assert registry.register("view", "Store Sales") == "store_sales"
    assert registry.register("view", "store_sales") == "store_sales_2"
    assert any("collision" in w for w in warnings)


def test_reserved_words_are_suffixed():
    warnings: list[str] = []
    assert NameRegistry(warnings).register("v", "sql") == "sql_field"
    assert any("reserved word" in w for w in warnings)


# --- golden files ----------------------------------------------------------


def test_output_matches_the_golden_project(emitted):
    """Byte-for-byte comparison against a checked-in project, to catch formatting
    regressions that `lkml` would happily parse. Regenerate deliberately:
    `pytest --regenerate-lookml-golden` is NOT provided on purpose - update the
    fixtures by hand so a diff is reviewed rather than rubber-stamped."""
    expected_root = FIXTURES / "lookml" / "expected"
    on_disk = {
        str(p.relative_to(expected_root)) for p in expected_root.rglob("*.lkml") if p.is_file()
    }
    assert on_disk == set(emitted.files), "golden file set drifted from emitter output"
    for name, content in sorted(emitted.files.items()):
        assert content == (expected_root / name).read_text(), f"{name} differs from its golden copy"


# --- multi-fact models -----------------------------------------------------


def test_each_fact_table_gets_its_own_star_explore(retail_model):
    """The bundled retail model has two fact tables sharing five conformed
    dimensions. Joining both into one explore would be a chasm trap - the facts
    would multiply each other's rows - so each gets its own explore, matching the
    model's own documented "query sales and returns separately" guidance."""
    result = emit_lookml_project(retail_model, connection="retail_wh")
    model_file = next(name for name in result.files if name.endswith(".model.lkml"))
    explores = {e["name"]: e for e in lkml.load(result.files[model_file])["explores"]}

    assert set(explores) == {"fct_store_sales", "fct_store_returns"}
    for explore in explores.values():
        joined = {j["name"] for j in explore["joins"]}
        assert not any(name.startswith("fct_") for name in joined), (
            f"explore {explore['name']!r} joins another fact table: {joined}"
        )
        assert all(j["relationship"] == "many_to_one" for j in explore["joins"])


def test_every_relationship_is_followed_only_in_its_declared_direction(retail_model):
    """An Ossie relationship is many-to-one from `from` to `to`, so a star built
    this way never fans out the root."""
    result = emit_lookml_project(retail_model, connection="retail_wh")
    model_file = next(name for name in result.files if name.endswith(".model.lkml"))
    declared = {(r.from_dataset, r.to) for r in retail_model.relationships}
    for explore in lkml.load(result.files[model_file])["explores"]:
        for join in explore["joins"]:
            assert any(to == join["name"] for _, to in declared)


def test_all_retail_views_and_explores_parse(retail_model):
    result = emit_lookml_project(retail_model, connection="retail_wh")
    for content in result.files.values():
        lkml.load(content)


def test_dataset_without_a_primary_key_warns_about_symmetric_aggregates(tpcds_model):
    """Without a primary_key dimension Looker cannot apply symmetric aggregates, so
    any measure on that view silently goes wrong once a join fans out its rows.
    That has to be warned about, not passed over."""
    from lexis._vendor.ossie import OssieSemanticModel
    from lexis.resolved_model import ResolvedModel

    source = tpcds_model.semantic_model
    keyless = [
        d.model_copy(update={"primary_key": None, "unique_keys": None})
        if d.name == "item"
        else d
        for d in source.datasets
    ]
    model = ResolvedModel.build(
        OssieSemanticModel(
            name=source.name,
            datasets=keyless,
            relationships=source.relationships,
            metrics=source.metrics,
        )
    )
    result = emit_lookml_project(model, connection="c")

    warning = next(w for w in result.warnings if "'item'" in w and "primary key" in w)
    assert "symmetric aggregates" in warning

    item = next(v for v in lkml.load(result.files["views/item.view.lkml"])["views"])
    assert not any("primary_key" in d for d in item.get("dimensions", []))
