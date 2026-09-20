"""Shared target-dispatch logic: pick the right emitter for a transpile request.

Used by both the CLI (`cli.py`) and the web API (`lexis_api`) so the mapping from
`--target` to an emitter lives in exactly one place.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from lexis._vendor.ossie import OssieDialect, OssieDocument
from lexis.resolved_model import ResolvedModel
from lexis.sml.emit import emit_sml_files
from lexis.transpilers.cube import emit_cube_yaml
from lexis.transpilers.dbt_ossie import emit_dbt_ossie_document
from lexis.transpilers.lookml import emit_lookml_project
from lexis.transpilers.mcp import emit_mcp_tool_manifest
from lexis.transpilers.snowflake_semantic_view import emit_snowflake_semantic_view
from lexis.transpilers.sql import EMITTERS as SQL_EMITTERS

TARGETS = [*SQL_EMITTERS.keys(), "cube", "dbt", "mcp", "snowflake_semantic_view", "sml", "lookml"]

# Per-target options accepted via `transpile(..., options=...)`. Anything a target
# needs that Ossie itself doesn't model - so far only LookML, which requires a
# Looker connection name and picks a SQL dialect for the expressions it embeds.
TARGET_OPTIONS = {"lookml": frozenset({"connection", "dialect"})}

# Short alternate spellings accepted alongside the canonical TARGETS name - resolved
# to the canonical name before dispatch, so callers/tests only ever need to branch on
# the canonical spelling below.
TARGET_ALIASES = {"ssv": "snowflake_semantic_view"}


@dataclass(frozen=True)
class TranspileResult:
    # `sml` is the one multi-file target - one YAML file per SML object - so
    # `content` is a `dict[str, str]` (relative filename -> content) there;
    # every other target still returns a single `str`.
    content: str | dict[str, str]
    warnings: list[str]


def _lookml_options(options: Mapping[str, str]) -> dict:
    """Validate and coerce the LookML target's options into emitter kwargs."""
    kwargs: dict = {}
    if "connection" in options:
        connection = str(options["connection"]).strip()
        if not connection:
            raise ValueError("lookml option 'connection' must not be empty")
        kwargs["connection"] = connection
    if "dialect" in options:
        raw = str(options["dialect"]).strip().upper()
        try:
            kwargs["dialect"] = OssieDialect(raw)
        except ValueError:
            valid = ", ".join(d.value for d in OssieDialect)
            raise ValueError(f"unknown lookml dialect {raw!r}; expected one of: {valid}") from None
    return kwargs


def transpile(
    document: OssieDocument,
    model: ResolvedModel,
    target: str,
    metric: str | None = None,
    group_by: list[str] | None = None,
    options: Mapping[str, str] | None = None,
) -> TranspileResult:
    """Emit `model` (parsed from `document`) in the given `target` format.

    `options` carries per-target settings that Ossie itself doesn't model (see
    TARGET_OPTIONS); passing a key the target doesn't accept is an error rather
    than a silent no-op, so a typo surfaces immediately.

    Raises ValueError for a missing/unknown target, a missing metric on a SQL
    target, or an unaccepted option.
    """
    target = TARGET_ALIASES.get(target, target)

    options = dict(options or {})
    if options:
        accepted = TARGET_OPTIONS.get(target, frozenset())
        unknown = sorted(set(options) - accepted)
        if unknown:
            expected = ", ".join(sorted(accepted)) if accepted else "none"
            raise ValueError(
                f"target {target!r} does not accept option(s) {unknown}; accepted: {expected}"
            )

    if target in SQL_EMITTERS:
        if not metric:
            raise ValueError(f"metric is required for target {target!r}")
        emitter = SQL_EMITTERS[target]()
        content = emitter.emit_metric_query(model, metric, group_by=group_by or None)
        return TranspileResult(content=content, warnings=[])
    elif target == "cube":
        return TranspileResult(content=emit_cube_yaml(model), warnings=[])
    elif target == "dbt":
        result = emit_dbt_ossie_document(document)
        return TranspileResult(content=result.artifact.content, warnings=result.warnings)
    elif target == "mcp":
        return TranspileResult(content=emit_mcp_tool_manifest(model), warnings=[])
    elif target == "snowflake_semantic_view":
        return TranspileResult(content=emit_snowflake_semantic_view(model), warnings=[])
    elif target == "lookml":
        result = emit_lookml_project(model, **_lookml_options(options))
        return TranspileResult(content=result.files, warnings=result.warnings)
    elif target == "sml":
        result = emit_sml_files(document)
        return TranspileResult(content=result.files, warnings=result.warnings)
    else:
        raise ValueError(f"Unknown target {target!r}")
