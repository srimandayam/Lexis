"""Pydantic request/response schemas for the API.

Deliberately separate from the vendored `lexis._vendor.ossie` pydantic models —
the HTTP contract shouldn't be coupled to (and broken by) internal vendored-library
shape changes. `to_*_out` helpers below map Ossie objects into these schemas.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from lexis._vendor.ossie import OssieDataset, OssieDialect, OssieMetric, OssieRelationship
from lexis.resolved_model import MissingExpressionError, ResolvedModel
from lexis_api.models import Connection, SemanticModelRecord

TARGET = Literal[
    "duckdb",
    "postgres",
    "bigquery",
    "databricks",
    "snowflake",
    "cube",
    "dbt",
    "mcp",
    "snowflake_semantic_view",
    "sml",
    "lookml",
]

TIME_GRAIN = Literal["year", "quarter", "month", "day"]

CONNECTION_TYPE = Literal["duckdb_file", "snowflake"]


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    # Set only for a Google-authenticated user (see deps.find_or_create_google_user);
    # null for the seeded dev-stub users. The frontend treats its presence on
    # `/api/users/me` as "this is a real signed-in identity, not the dev switcher".
    email: str | None = None


class FieldOut(BaseModel):
    name: str
    description: str | None = None
    expression: str | None = None  # ANSI_SQL text, best-effort (None if unavailable)
    is_time: bool = False  # from OssieField.is_time_dimension() - candidate for time-series grain grouping
    datatype: str | None = None  # OssieDataType value (e.g. "String", "Integer"), if the model declares one


class DatasetOut(BaseModel):
    name: str
    source: str
    fields: list[FieldOut]


class RelationshipOut(BaseModel):
    name: str
    from_dataset: str
    to: str
    from_columns: list[str]
    to_columns: list[str]


class MetricOut(BaseModel):
    name: str
    description: str | None = None
    expression: str | None = None  # ANSI_SQL text, best-effort (None if unavailable)
    referenced_datasets: list[str] = []
    datatype: str | None = None  # OssieDataType value (e.g. "Decimal", "Integer"), if the model declares one


class ModelSummaryOut(BaseModel):
    id: int
    name: str
    owner_id: int
    owner_username: str
    dataset_count: int
    metric_count: int
    created_at: datetime
    updated_at: datetime


class ModelDetailOut(ModelSummaryOut):
    raw_yaml: str
    datasets: list[DatasetOut]
    relationships: list[RelationshipOut]
    metrics: list[MetricOut]


class CreateModelIn(BaseModel):
    name: str | None = None
    yaml_text: str


class UpdateModelIn(BaseModel):
    yaml_text: str


class ImportSmlOut(BaseModel):
    """Response for importing an SML repo as a new model - same shape a normal
    `POST /api/models` returns, plus any `parse_sml_repo` warnings (unsupported
    object types, unconvertible metric_calc MDX, ...) the caller should surface."""

    model: ModelDetailOut
    warnings: list[str]


class TranspileIn(BaseModel):
    target: TARGET
    metric: str | None = None
    group_by: list[str] | None = None
    # Per-target settings Ossie itself doesn't model - currently only `lookml`,
    # which needs a Looker connection name and picks a SQL dialect for the
    # expressions it embeds. Validated against dispatch.TARGET_OPTIONS, so an
    # option a target doesn't accept is a 422 rather than a silent no-op.
    options: dict[str, str] | None = None


class TranspileOut(BaseModel):
    # `sml` and `lookml` are the multi-file targets - one YAML file per SML
    # object, and a views/ + model file project respectively - so `content` is a
    # `dict[str, str]` (relative filename -> content) there; every other target
    # still returns a single `str`. Mirrors dispatch.TranspileResult.
    content: str | dict[str, str]
    warnings: list[str]


class RunDuckDbOut(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    sql: str


class GraphFieldIn(BaseModel):
    name: str
    expression: str  # ANSI_SQL text
    description: str | None = None


class GraphDatasetIn(BaseModel):
    name: str
    source: str
    fields: list[GraphFieldIn] = []


class GraphRelationshipIn(BaseModel):
    name: str
    from_dataset: str
    to: str
    from_columns: list[str]
    to_columns: list[str]


class GraphMetricIn(BaseModel):
    name: str
    expression: str  # ANSI_SQL text
    description: str | None = None


class GraphEditIn(BaseModel):
    """Structured graph-canvas state for a model's datasets/relationships/metrics.
    Applied by merging onto the existing parsed OssieDocument (see graph_edit.py) so
    attributes the canvas has no control for (ai_context, custom_extensions,
    non-ANSI_SQL dialect expressions, primary_key/unique_keys) are preserved
    untouched."""

    datasets: list[GraphDatasetIn]
    relationships: list[GraphRelationshipIn] = []
    metrics: list[GraphMetricIn] = []


class ConnectionIn(BaseModel):
    """`config` shape depends on `type` - validated in
    `connection_runtime.validate_connection_config` rather than as a pydantic
    discriminated union, since it's just two flat shapes:

    - duckdb_file: `{"path": "<server-side filesystem path>"}`
    - snowflake: `{"account", "user", "password_env", "warehouse"?, "database"?,
      "schema"?, "role"?}` - `password_env` is the *name* of an environment variable
      the API process reads at connect time; the real secret is never stored here.
    """

    name: str
    type: CONNECTION_TYPE
    config: dict[str, Any]


class ConnectionOut(BaseModel):
    id: int
    name: str
    type: CONNECTION_TYPE
    owner_id: int
    owner_username: str
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ConnectionTestOut(BaseModel):
    ok: bool
    detail: str


def to_connection_out(conn: Connection) -> ConnectionOut:
    return ConnectionOut(
        id=conn.id,
        name=conn.name,
        type=conn.type.value,
        owner_id=conn.owner_id,
        owner_username=conn.owner.username,
        config=conn.config,
        created_at=conn.created_at,
        updated_at=conn.updated_at,
    )


def _expression_text(model: ResolvedModel, expressed) -> str | None:
    """Best-effort ANSI_SQL text for anything with an `.expression: OssieExpression`
    attribute (OssieField or OssieMetric)."""
    try:
        return model.resolve_expression(expressed.expression, OssieDialect.ANSI_SQL)
    except MissingExpressionError:
        return None


def to_dataset_out(dataset: OssieDataset, model: ResolvedModel) -> DatasetOut:
    return DatasetOut(
        name=dataset.name,
        source=dataset.source,
        fields=[
            FieldOut(
                name=f.name,
                description=f.description,
                expression=_expression_text(model, f),
                is_time=f.is_time_dimension(),
                datatype=f.datatype.value if f.datatype else None,
            )
            for f in dataset.fields or []
        ],
    )


def to_relationship_out(rel: OssieRelationship) -> RelationshipOut:
    return RelationshipOut(
        name=rel.name,
        from_dataset=rel.from_dataset,
        to=rel.to,
        from_columns=rel.from_columns,
        to_columns=rel.to_columns,
    )


def to_metric_out(metric: OssieMetric, model: ResolvedModel) -> MetricOut:
    expression = _expression_text(model, metric)
    return MetricOut(
        name=metric.name,
        description=metric.description,
        expression=expression,
        referenced_datasets=model.referenced_datasets(expression) if expression else [],
        datatype=metric.datatype.value if metric.datatype else None,
    )


def to_summary_out(record: SemanticModelRecord, model: ResolvedModel) -> ModelSummaryOut:
    return ModelSummaryOut(
        id=record.id,
        name=record.name,
        owner_id=record.owner_id,
        owner_username=record.owner.username,
        dataset_count=len(model.datasets),
        metric_count=len(model.metrics),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_detail_out(record: SemanticModelRecord, model: ResolvedModel) -> ModelDetailOut:
    summary = to_summary_out(record, model)
    return ModelDetailOut(
        **summary.model_dump(),
        raw_yaml=record.raw_yaml,
        datasets=[to_dataset_out(d, model) for d in model.datasets.values()],
        relationships=[to_relationship_out(r) for r in model.relationships],
        metrics=[to_metric_out(m, model) for m in model.metrics.values()],
    )
