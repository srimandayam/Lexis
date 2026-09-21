"""CLI tests for `lexis export-demo-dataset` (the `transpile` command is
exercised end-to-end via the README's own examples, not unit-tested here)."""

import duckdb
import pytest
from click.testing import CliRunner

from lexis.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def test_export_demo_dataset_writes_a_working_duckdb_file(runner, tmp_path):
    out_path = tmp_path / "demo.duckdb"
    result = runner.invoke(main, ["export-demo-dataset", "--out", str(out_path)])

    assert result.exit_code == 0, result.output
    assert out_path.exists()

    # Connecting to the file directly (rather than the app's `ATTACH ... AS tpcds`
    # pattern) exposes its data under its own default catalog, so no `tpcds.` prefix.
    con = duckdb.connect(str(out_path), read_only=True)
    rows = con.execute("SELECT SUM(ss_ext_sales_price) FROM public.store_sales").fetchall()
    con.close()
    assert rows == [(260.0,)]


def test_export_demo_dataset_retail_writes_the_large_dataset(runner, tmp_path):
    out_path = tmp_path / "retail.duckdb"
    result = runner.invoke(
        main, ["export-demo-dataset", "--dataset", "retail", "--out", str(out_path)]
    )

    assert result.exit_code == 0, result.output
    con = duckdb.connect(str(out_path), read_only=True)
    (sales,) = con.execute("SELECT COUNT(*) FROM public.fct_store_sales").fetchone()
    (returns,) = con.execute("SELECT COUNT(*) FROM public.fct_store_returns").fetchone()
    con.close()
    assert sales == 10_000
    assert returns == 1_500


def test_export_demo_dataset_rejects_an_unknown_dataset(runner, tmp_path):
    result = runner.invoke(
        main, ["export-demo-dataset", "--dataset", "nope", "--out", str(tmp_path / "x.duckdb")]
    )
    assert result.exit_code != 0


def test_export_demo_dataset_refuses_to_overwrite_without_force(runner, tmp_path):
    out_path = tmp_path / "demo.duckdb"
    out_path.write_bytes(b"not a real duckdb file")

    result = runner.invoke(main, ["export-demo-dataset", "--out", str(out_path)])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_export_demo_dataset_force_overwrites(runner, tmp_path):
    out_path = tmp_path / "demo.duckdb"
    out_path.write_bytes(b"not a real duckdb file")

    result = runner.invoke(main, ["export-demo-dataset", "--out", str(out_path), "--force"])
    assert result.exit_code == 0, result.output

    con = duckdb.connect(str(out_path), read_only=True)
    con.close()


def test_transpile_lookml_writes_a_project_tree(runner, tmp_path):
    out_dir = tmp_path / "lookml"
    result = runner.invoke(
        main,
        [
            "transpile",
            "tests/fixtures/tpcds_semantic_model.yaml",
            "--target",
            "lookml",
            "--lookml-connection",
            "tpcds_warehouse",
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    model_file = out_dir / "tpcds_retail_model.model.lkml"
    assert model_file.exists()
    assert 'connection: "tpcds_warehouse"' in model_file.read_text()
    assert (out_dir / "views" / "store_sales.view.lkml").exists()


def test_transpile_lookml_without_a_connection_uses_the_placeholder(runner, tmp_path):
    out_dir = tmp_path / "lookml"
    result = runner.invoke(
        main,
        ["transpile", "tests/fixtures/tpcds_semantic_model.yaml", "--target", "lookml", "--out", str(out_dir)],
    )

    assert result.exit_code == 0, result.output
    assert 'connection: "lexis_connection"' in (out_dir / "tpcds_retail_model.model.lkml").read_text()
    assert "lexis_connection" in result.output  # the warning, on stderr


def test_transpile_lookml_dialect_flag_is_passed_through(runner, tmp_path):
    out_dir = tmp_path / "lookml"
    result = runner.invoke(
        main,
        [
            "transpile",
            "tests/fixtures/tpcds_semantic_model.yaml",
            "--target",
            "lookml",
            "--lookml-dialect",
            "bigquery",
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "CONCAT(" in (out_dir / "views" / "store_sales.view.lkml").read_text()


def test_transpile_rejects_an_unknown_lookml_dialect(runner):
    result = runner.invoke(
        main,
        [
            "transpile",
            "tests/fixtures/tpcds_semantic_model.yaml",
            "--target",
            "lookml",
            "--lookml-dialect",
            "klingon",
        ],
    )
    assert result.exit_code != 0
    assert "unknown lookml dialect" in result.output


def test_lookml_flag_on_another_target_is_an_error_not_a_silent_no_op(runner):
    """A flag that can't apply should fail loudly rather than be ignored."""
    result = runner.invoke(
        main,
        [
            "transpile",
            "tests/fixtures/tpcds_semantic_model.yaml",
            "--target",
            "cube",
            "--lookml-connection",
            "x",
        ],
    )
    assert result.exit_code != 0
    assert "does not accept option" in result.output
