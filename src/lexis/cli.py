"""Lexis CLI: `lexis transpile <model.yaml> --target <target> ...`"""

import os
import re
from contextlib import contextmanager
from pathlib import Path

import click

from lexis.dispatch import TARGET_ALIASES, TARGETS
from lexis.dispatch import transpile as dispatch_transpile
from lexis.parser import load_ossie_document
from lexis.resolved_model import ResolvedModel

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@click.group()
def main() -> None:
    """Lexis: transpile Ossie semantic models to warehouse SQL and BI/AI formats."""


@main.command()
@click.argument("model_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--target", type=click.Choice([*TARGETS, *TARGET_ALIASES]), required=True)
@click.option("--metric", help="Metric name (required for SQL targets)")
@click.option(
    "--group-by",
    multiple=True,
    metavar="DATASET.FIELD",
    help="Field to group by, e.g. item.i_category (SQL targets only, repeatable)",
)
@click.option(
    "--out",
    type=click.Path(),
    help="Write output to a file (single-file targets) or a directory (multi-file "
    "targets, e.g. sml, lookml) instead of stdout",
)
@click.option(
    "--lookml-connection",
    metavar="NAME",
    help="Looker connection name for the model file (lookml target only). Ossie "
    "does not model a connection, so without this the project is emitted with a "
    "placeholder Looker cannot run queries against.",
)
@click.option(
    "--lookml-dialect",
    metavar="DIALECT",
    help="Ossie dialect whose expressions the LookML project embeds, e.g. "
    "SNOWFLAKE or BIGQUERY (lookml target only). Defaults to ANSI_SQL.",
)
def transpile(
    model_path: str,
    target: str,
    metric: str | None,
    group_by: tuple[str, ...],
    out: str | None,
    lookml_connection: str | None,
    lookml_dialect: str | None,
) -> None:
    """Parse an Ossie model and emit it in the given TARGET format."""
    document = load_ossie_document(model_path)
    semantic_model = document.semantic_model[0]
    model = ResolvedModel.build(semantic_model)

    # Only send options the caller actually set, so dispatch's unknown-option
    # check still rejects `--lookml-connection` paired with a non-lookml target
    # rather than silently ignoring it.
    options = {
        key: value
        for key, value in (("connection", lookml_connection), ("dialect", lookml_dialect))
        if value is not None
    }

    try:
        result = dispatch_transpile(
            document, model, target, metric, list(group_by) or None, options or None
        )
    except ValueError as exc:
        raise click.UsageError(str(exc))

    for warning in result.warnings:
        click.echo(f"warning: {warning}", err=True)

    if isinstance(result.content, dict):
        if out:
            out_dir = Path(out)
            for filename, content in result.content.items():
                path = out_dir / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            click.echo(f"Wrote {len(result.content)} file(s) to {out_dir}/", err=True)
        else:
            for filename, content in result.content.items():
                click.echo(f"# --- {filename} ---")
                click.echo(content)
    elif out:
        Path(out).write_text(result.content)
        click.echo(f"Wrote {out}", err=True)
    else:
        click.echo(result.content)


@main.group("import")
def import_group() -> None:
    """Import a third-party semantic model format into an Ossie document."""


@import_group.command("sml")
@click.argument("repo_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--out", type=click.Path(), required=True, help="Path to write the resulting Ossie YAML document")
def import_sml(repo_dir: str, out: str) -> None:
    """Parse an SML repo directory (REPO_DIR) into an Ossie YAML document."""
    from lexis.sml._common import ConversionError
    from lexis.sml.parse import parse_sml_repo

    try:
        result = parse_sml_repo(repo_dir)
    except ConversionError as exc:
        raise click.UsageError(str(exc))

    for warning in result.warnings:
        click.echo(f"warning: {warning}", err=True)

    Path(out).write_text(result.document.to_ossie_yaml())
    click.echo(f"Wrote {out}", err=True)


@main.command("export-demo-dataset")
@click.option("--out", type=click.Path(dir_okay=False), required=True, help="Path to write the .duckdb file")
@click.option(
    "--dataset",
    type=click.Choice(["tpcds", "retail"]),
    default="tpcds",
    show_default=True,
    help="Which bundled demo dataset to export: the small TPC-DS fixture, or the "
    "larger retail analytics dataset (10,000 sales facts + returns)",
)
@click.option("--force", is_flag=True, help="Overwrite --out if it already exists")
def export_demo_dataset_cmd(out: str, dataset: str, force: bool) -> None:
    """Write a bundled demo dataset - the same data the web UI's "Demo dataset" run
    mode uses - to a real .duckdb file, so it can be re-uploaded (Run tab's Upload
    mode) or registered as a duckdb_file connection."""
    try:
        from lexis.demo_data import export_demo_dataset
        from lexis.retail_demo_data import export_retail_demo_dataset
    except ImportError as exc:
        raise click.UsageError(
            'exporting the demo dataset requires duckdb - install with `pip install "lexis-cli[mcp]"`'
        ) from exc

    exporter = export_retail_demo_dataset if dataset == "retail" else export_demo_dataset
    try:
        exporter(out, overwrite=force)
    except FileExistsError as exc:
        raise click.UsageError(f"{exc} (pass --force to overwrite)")

    click.echo(f"Wrote {out}", err=True)


@contextmanager
def _demo_connection(model: ResolvedModel):
    try:
        from lexis.demo_data import build_tpcds_demo_connection
        from lexis.retail_demo_data import build_retail_demo_connection
    except ImportError as exc:
        raise click.UsageError(
            '--demo requires duckdb - install with `pip install "lexis-cli[mcp]"`'
        ) from exc

    # Pick the bundled demo whose fixture schema matches the model's `source`
    # catalog. Unlike the API's `/run` endpoint (which checks demo-compatibility
    # per request, for just the one metric being queried), we don't know which
    # metric will be called until a tool call arrives, so we can't reject an
    # individual incompatible metric up front - let it fail naturally with DuckDB's
    # own "table/catalog not found" error when it's actually invoked.
    catalogs = {ds.source.split(".", 1)[0] for ds in model.datasets.values()}
    builder = build_retail_demo_connection if catalogs == {"retail"} else build_tpcds_demo_connection

    con = builder()
    try:
        yield con
    finally:
        con.close()


@contextmanager
def _duckdb_file_connection(model: ResolvedModel, path: str):
    try:
        import duckdb
    except ImportError as exc:
        raise click.UsageError(
            '--duckdb-file requires duckdb - install with `pip install "lexis-cli[mcp]"`'
        ) from exc

    catalogs = {ds.source.split(".", 1)[0] for ds in model.datasets.values()}
    if len(catalogs) != 1:
        raise click.UsageError(
            "--duckdb-file requires every dataset in the model to share one catalog name "
            f"in its `source`; found {sorted(catalogs)}"
        )
    catalog = catalogs.pop()
    if not _SAFE_IDENTIFIER_RE.match(catalog):
        raise click.UsageError(f"invalid catalog name in source: {catalog!r}")

    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{path}' AS {catalog} (READ_ONLY)")
        yield con
    finally:
        con.close()


@contextmanager
def _snowflake_connection(
    account: str,
    user: str,
    password_env: str,
    warehouse: str | None,
    database: str | None,
    schema: str | None,
    role: str | None,
):
    try:
        import snowflake.connector
    except ImportError as exc:
        raise click.UsageError(
            '--snowflake-account requires snowflake-connector-python - install with '
            '`pip install "lexis-cli[mcp]"`'
        ) from exc

    password = os.environ.get(password_env)
    if not password:
        raise click.UsageError(f"environment variable {password_env!r} (--snowflake-password-env) is not set")

    con = snowflake.connector.connect(
        account=account,
        user=user,
        password=password,
        warehouse=warehouse,
        database=database,
        schema=schema,
        role=role,
    )
    try:
        yield con
    finally:
        con.close()


@main.command("mcp-serve")
@click.argument("model_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--demo", is_flag=True, help="Run against the bundled TPC-DS demo dataset")
@click.option("--duckdb-file", type=click.Path(exists=True, dir_okay=False), help="Run against a local .duckdb file")
@click.option("--snowflake-account", help="Run against Snowflake (requires --snowflake-user/-password-env)")
@click.option("--snowflake-user")
@click.option("--snowflake-password-env", help="Env var holding the Snowflake password")
@click.option("--snowflake-warehouse")
@click.option("--snowflake-database")
@click.option("--snowflake-schema")
@click.option("--snowflake-role")
def mcp_serve(
    model_path: str,
    demo: bool,
    duckdb_file: str | None,
    snowflake_account: str | None,
    snowflake_user: str | None,
    snowflake_password_env: str | None,
    snowflake_warehouse: str | None,
    snowflake_database: str | None,
    snowflake_schema: str | None,
    snowflake_role: str | None,
) -> None:
    """Serve MODEL's metrics as live MCP tools over stdio (e.g. for Claude Desktop) -
    one `query_<metric>` tool per metric, executed against the demo dataset, a local
    DuckDB file, or Snowflake."""
    try:
        import anyio
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise click.UsageError(
            'mcp-serve requires the `mcp` package - install with `pip install "lexis-cli[mcp]"`'
        ) from exc

    if sum(bool(x) for x in (demo, duckdb_file, snowflake_account)) != 1:
        raise click.UsageError("pass exactly one of --demo, --duckdb-file, or --snowflake-account")

    document = load_ossie_document(model_path)
    model = ResolvedModel.build(document.semantic_model[0])

    from lexis import mcp_server as mcp_server_module
    from lexis.transpilers.sql import DuckDBEmitter, SnowflakeEmitter

    if demo:
        con_cm, emitter = _demo_connection(model), DuckDBEmitter()
    elif duckdb_file:
        con_cm, emitter = _duckdb_file_connection(model, duckdb_file), DuckDBEmitter()
    else:
        if not (snowflake_user and snowflake_password_env):
            raise click.UsageError("--snowflake-account requires --snowflake-user and --snowflake-password-env")
        con_cm = _snowflake_connection(
            snowflake_account,
            snowflake_user,
            snowflake_password_env,
            snowflake_warehouse,
            snowflake_database,
            snowflake_schema,
            snowflake_role,
        )
        emitter = SnowflakeEmitter()

    with con_cm as con:

        def execute(
            metric: str,
            group_by: list[str] | None,
            time_grain: str | None = None,
            time_field: str | None = None,
        ) -> dict:
            return mcp_server_module.run_metric_or_timeseries(
                con, emitter, model, metric, group_by, time_grain, time_field
            )

        server = mcp_server_module.build_server(model, execute)

        async def _run() -> None:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())

        anyio.run(_run)
