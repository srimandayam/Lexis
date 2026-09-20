"""Ossie -> LookML project emitter.

Looker models a semantic layer as a *project*: one `.view.lkml` per table
(dimensions + measures) plus a `.model.lkml` naming the database connection and
declaring `explore`s - rooted join trees over those views. This emitter produces
that file set from one `ResolvedModel`, so it is a multi-file target like `sml`
(`TranspileResult.content` is a `dict[str, str]`).

Three things LookML needs that Ossie does not carry, and how each is handled:

- **A connection name.** Every model file opens with `connection: "..."`. Ossie
  has no equivalent, so it comes in as the `connection` argument and defaults to
  a placeholder plus a warning.
- **Explores.** Ossie has a relationship *graph*; Looker needs rooted trees. Fact
  tables (datasets that are the `from` side of a relationship and never the `to`
  side) become explore roots, and relationships are followed only in their
  declared direction, so each explore is a pure star/snowflake with no join that
  fans out the root. Joins are emitted in BFS order, so every `sql_on` references
  a view already joined above it.
- **Primary keys.** Looker's symmetric aggregates - what keeps `SUM` correct when
  a join fans out rows - need exactly one `primary_key: yes` dimension per view.
  Ossie's `primary_key` is optional and may be composite, so a composite key is
  synthesized as a concatenation and a missing one is warned about loudly.

Metric decomposition reuses `lexis.sml._common`'s `decompose_simple_aggregate` /
`decompose_ratio_aggregate` rather than growing a third aggregate-matching regex
alongside the Cube emitter's and SML's.
"""

import re
from collections import deque
from dataclasses import dataclass, field

from lexis._vendor.ossie import (
    OssieAIContext,
    OssieAIContextObject,
    OssieDataset,
    OssieDataType,
    OssieDialect,
    OssieField,
)
from lexis.resolved_model import MissingExpressionError, ResolvedModel
from lexis.sml._common import decompose_ratio_aggregate, decompose_simple_aggregate
from lexis.transpilers.lookml.naming import NameRegistry, slugify
from lexis.transpilers.lookml.writer import (
    Bare,
    Block,
    Sql,
    bare_list,
    quoted_list,
    render_file,
)

DEFAULT_CONNECTION = "lexis_connection"

_SIMPLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")

#: Trailing fragments stripped from a temporal field's name to form its
#: `dimension_group` name, because Looker re-appends a timeframe suffix to every
#: generated field: group `created` yields `created_date`, `created_month`, ...
#: So a field literally named `d_date` must become group `d`, or its date field
#: would be `d_date_date`.
_TEMPORAL_NAME_SUFFIXES = ("_date", "_at", "_time", "_timestamp")

_DATE_TIMEFRAMES = ["raw", "date", "week", "month", "quarter", "year"]
_DATETIME_TIMEFRAMES = ["raw", "time", "date", "week", "month", "quarter", "year"]

_LOOKML_DIMENSION_TYPE = {
    OssieDataType.STRING: "string",
    OssieDataType.OPAQUE: "string",
    OssieDataType.INTEGER: "number",
    OssieDataType.DECIMAL: "number",
    OssieDataType.FLOAT: "number",
    OssieDataType.BOOLEAN: "yesno",
}

#: SML `calculation_method` (what the shared decomposer returns) -> LookML measure
#: type. `count non-null` is deliberately absent: LookML's `type: count` is a ROW
#: count that takes no `sql`, so a non-null column count has to be a `type: number`
#: measure wrapping `COUNT(...)` instead.
_LOOKML_MEASURE_TYPE = {
    "sum": "sum",
    "average": "average",
    "minimum": "min",
    "maximum": "max",
    "count distinct": "count_distinct",
}

#: Dialects whose string concatenation is `CONCAT(a, b)` rather than `a || b`,
#: used only for a synthesized composite primary key.
_CONCAT_FUNCTION_DIALECTS = frozenset({OssieDialect.BIGQUERY})


@dataclass(frozen=True)
class LookmlEmitResult:
    files: dict[str, str]
    warnings: list[str]


@dataclass
class _ViewPlan:
    """Everything known about one dataset's view before it is rendered."""

    dataset: OssieDataset
    slug: str
    dimensions: list[Block] = field(default_factory=list)
    measures: list[Block] = field(default_factory=list)
    #: Ossie column/field name -> the LookML field name to reference it by. For a
    #: dimension group this is the timeframe-suffixed name, not the group name.
    refs: dict[str, str] = field(default_factory=dict)
    has_primary_key: bool = False


def _ai_context_parts(ai_context: OssieAIContext | None) -> tuple[str | None, tuple[str, ...]]:
    """`(instructions, synonyms)` from either ai_context shape (str or object)."""
    if ai_context is None:
        return None, ()
    if isinstance(ai_context, OssieAIContextObject):
        return ai_context.instructions, tuple(ai_context.synonyms or ())
    return str(ai_context), ()


def _describe(base: str | None, ai_context: OssieAIContext | None) -> str | None:
    instructions, _ = _ai_context_parts(ai_context)
    parts = [p for p in (base, instructions) if p]
    return " ".join(parts) if parts else None


def _tags(ai_context: OssieAIContext | None) -> Bare | None:
    _, synonyms = _ai_context_parts(ai_context)
    return quoted_list(list(synonyms)) if synonyms else None


def _temporal_kind(ossie_field: OssieField) -> str | None:
    """`"date"`, `"datetime"`, or None.

    Keyed off `datatype` alone, deliberately NOT off `OssieField.is_time_dimension()`
    the way the Cube emitter is: the bundled TPC-DS fixture marks `d_year` (an
    integer) and `d_month_name` (a string) as `dimension.is_time: true`, and a
    LookML `type: time` dimension group over either produces SQL Looker cannot run.
    Cube's `type: time` is more forgiving than Looker's, so the two emitters differ
    here on purpose.
    """
    if ossie_field.datatype == OssieDataType.DATE:
        return "date"
    if ossie_field.datatype in (OssieDataType.DATE_TIME, OssieDataType.DATE_TIME_TZ):
        return "datetime"
    return None


def _group_base_name(field_slug: str) -> str:
    for suffix in _TEMPORAL_NAME_SUFFIXES:
        if field_slug.endswith(suffix) and len(field_slug) > len(suffix):
            return field_slug[: -len(suffix)]
    return field_slug


def _column_sql(expression: str, dataset_name: str, warnings: list[str]) -> Sql:
    """Scope a field expression to its own view.

    A bare column name is qualified with `${TABLE}`; anything else (a multi-column
    scalar expression like `c_first_name || ' ' || c_last_name`) is passed through
    verbatim, because rewriting it correctly needs a real SQL parser. Same rule
    `SqlDialectEmitter._resolve_field_expr` already applies for generated SQL.
    """
    if _SIMPLE_IDENTIFIER_RE.match(expression):
        return Sql("${TABLE}." + expression)
    warnings.append(
        f"dataset {dataset_name!r}: expression {expression!r} is not a bare column, "
        "so its columns are left unqualified - verify it is unambiguous once the "
        "view is joined in an explore."
    )
    return Sql(expression)


def _referenced_columns(model: ResolvedModel, dialect: OssieDialect) -> dict[str, set[str]]:
    """Dataset -> columns that some metric expression references as `dataset.column`.

    Collected up front so a referenced column with no declared Ossie field still
    gets a (hidden) dimension to point `${view.field}` at.
    """
    needed: dict[str, set[str]] = {name: set() for name in model.datasets}
    for metric in model.metrics.values():
        try:
            expression = model.resolve_expression(metric.expression, dialect)
        except MissingExpressionError:
            continue
        for dataset_name, column in _QUALIFIED_REF_RE.findall(expression):
            if dataset_name in needed:
                needed[dataset_name].add(column)
    return needed


def _join_columns(model: ResolvedModel) -> dict[str, set[str]]:
    """Dataset -> columns used on either side of a relationship."""
    needed: dict[str, set[str]] = {name: set() for name in model.datasets}
    for rel in model.relationships:
        if rel.from_dataset in needed:
            needed[rel.from_dataset].update(rel.from_columns)
        if rel.to in needed:
            needed[rel.to].update(rel.to_columns)
    return needed


def _build_dimensions(
    plan: _ViewPlan,
    model: ResolvedModel,
    names: NameRegistry,
    dialect: OssieDialect,
    extra_columns: set[str],
    warnings: list[str],
) -> None:
    """Populate `plan.dimensions` / `plan.refs` from the dataset's Ossie fields,
    plus hidden dimensions for any column referenced elsewhere but never declared."""
    dataset = plan.dataset
    ossie_fields = list(dataset.fields or [])
    primary_key = list(dataset.primary_key or [])
    single_key = primary_key[0] if len(primary_key) == 1 else None

    declared: set[str] = set()

    for ossie_field in ossie_fields:
        declared.add(ossie_field.name)
        try:
            expression = model.resolve_expression(ossie_field.expression, dialect)
        except MissingExpressionError:
            warnings.append(
                f"LOSSY: dataset {dataset.name!r} field {ossie_field.name!r} has no "
                f"{dialect.value} or ANSI_SQL expression; omitted from the LookML view."
            )
            continue

        sql = _column_sql(expression, dataset.name, warnings)
        description = _describe(ossie_field.description, ossie_field.ai_context)
        tags = _tags(ossie_field.ai_context)
        temporal = _temporal_kind(ossie_field)

        if ossie_field.datatype == OssieDataType.TIME:
            warnings.append(
                f"dataset {dataset.name!r} field {ossie_field.name!r} has datatype Time; "
                "LookML has no time-of-day dimension type, so it is emitted as a string."
            )

        if temporal is None:
            if ossie_field.is_time_dimension() and ossie_field.datatype != OssieDataType.TIME:
                warnings.append(
                    f"dataset {dataset.name!r} field {ossie_field.name!r} is marked "
                    "dimension.is_time but its datatype is not temporal; emitted as a "
                    "plain dimension rather than a dimension_group, since a LookML "
                    "`type: time` group over a non-temporal column produces invalid SQL."
                )
            name = names.register(plan.slug, ossie_field.name)
            block = Block("dimension", name)
            lookml_type = _LOOKML_DIMENSION_TYPE.get(ossie_field.datatype) if ossie_field.datatype else None
            block.add("type", Bare(lookml_type) if lookml_type else None)
            block.add("label", ossie_field.label)
            block.add("description", description)
            block.add("tags", tags)
            if single_key == ossie_field.name:
                block.add("primary_key", Bare("yes"))
                plan.has_primary_key = True
            block.add("sql", sql)
            plan.dimensions.append(block)
            plan.refs[ossie_field.name] = name
            continue

        # Temporal: a dimension_group, whose generated fields are `<group>_<timeframe>`.
        base = _group_base_name(slugify(ossie_field.name))
        if not base or names.is_taken(plan.slug, base):
            base = f"{slugify(ossie_field.name)}_group"
        group_name = names.register(plan.slug, ossie_field.name, preferred=base)
        timeframe = "date" if temporal == "date" else "raw"
        reference = f"{group_name}_{timeframe}"

        block = Block("dimension_group", group_name)
        block.add("type", Bare("time"))
        block.add(
            "timeframes",
            bare_list(_DATE_TIMEFRAMES if temporal == "date" else _DATETIME_TIMEFRAMES),
        )
        if temporal == "date":
            block.add("convert_tz", Bare("no"))
            block.add("datatype", Bare("date"))
        block.add("label", ossie_field.label)
        block.add("description", description)
        block.add("tags", tags)
        block.add("sql", sql)
        block.comments.append(
            f"Ossie field {ossie_field.name!r}; reference it as ${{{plan.slug}.{reference}}}."
        )
        plan.dimensions.append(block)
        plan.refs[ossie_field.name] = reference

        if single_key == ossie_field.name:
            warnings.append(
                f"dataset {dataset.name!r} primary key {ossie_field.name!r} is a temporal "
                "field, which LookML models as a dimension_group and cannot mark "
                "`primary_key`; a hidden key dimension was synthesized instead."
            )
            key_name = names.register(plan.slug, f"{ossie_field.name}__pk", preferred=f"{group_name}_pk")
            key_block = Block("dimension", key_name)
            key_block.add("primary_key", Bare("yes"))
            key_block.add("hidden", Bare("yes"))
            key_block.add("sql", sql)
            plan.dimensions.append(key_block)
            plan.has_primary_key = True

    # Columns referenced by joins, keys or metrics but never declared as a field.
    for column in sorted(extra_columns - declared):
        if not _SIMPLE_IDENTIFIER_RE.match(column):
            continue
        name = names.register(plan.slug, column)
        block = Block("dimension", name)
        block.add("hidden", Bare("yes"))
        block.add("sql", Sql("${TABLE}." + column))
        block.comments.append(
            "Referenced by a join, key or metric but not declared as an Ossie field."
        )
        plan.dimensions.append(block)
        plan.refs[column] = name

    _add_primary_key(plan, names, dialect, warnings)


def _add_primary_key(
    plan: _ViewPlan, names: NameRegistry, dialect: OssieDialect, warnings: list[str]
) -> None:
    """Synthesize a composite key dimension, or warn that the view has none."""
    primary_key = list(plan.dataset.primary_key or [])

    if len(primary_key) > 1:
        separator = "'-'"
        if dialect in _CONCAT_FUNCTION_DIALECTS:
            expression = "CONCAT(" + f", {separator}, ".join(
                "${TABLE}." + c for c in primary_key
            ) + ")"
        else:
            expression = f" || {separator} || ".join("${TABLE}." + c for c in primary_key)
        name = names.register(plan.slug, "__lexis_primary_key", preferred="lexis_primary_key")
        block = Block("dimension", name)
        block.add("primary_key", Bare("yes"))
        block.add("hidden", Bare("yes"))
        block.add("sql", Sql(expression))
        block.comments.append(
            f"Ossie primary_key is composite {primary_key}; LookML allows only one "
            "primary_key dimension, so it is synthesized as a concatenation."
        )
        block.comments.append(
            "Non-string key columns may need an explicit CAST on your warehouse."
        )
        plan.dimensions.insert(0, block)
        plan.has_primary_key = True
        warnings.append(
            f"LOSSY: dataset {plan.dataset.name!r} has a composite primary key "
            f"{primary_key}; emitted as a concatenated hidden dimension "
            f"{name!r} because LookML permits only one primary_key dimension."
        )
        return

    if len(primary_key) == 1 and not plan.has_primary_key:
        warnings.append(
            f"dataset {plan.dataset.name!r} primary key {primary_key[0]!r} does not "
            "match any emitted field; no primary_key dimension was set."
        )

    if not plan.has_primary_key:
        warnings.append(
            f"dataset {plan.dataset.name!r} has no usable primary key, so its view has "
            "no `primary_key` dimension. Looker's symmetric aggregates cannot protect "
            "measures on this view: any measure it carries will be wrong in an explore "
            "where a join fans out its rows. Add `primary_key` to the Ossie dataset."
        )

    if plan.dataset.unique_keys:
        warnings.append(
            f"LOSSY: dataset {plan.dataset.name!r} unique_keys "
            f"{plan.dataset.unique_keys} have no LookML equivalent and were dropped."
        )


def _rewrite_references(expression: str, plans: dict[str, _ViewPlan]) -> str:
    """Rewrite Ossie `dataset.column` refs into LookML `${view.field}` refs.

    Looks every reference up through the view plans rather than re-slugifying, so
    a name the registry had to suffix for a collision is referenced consistently.
    """

    def replace(match: re.Match[str]) -> str:
        dataset_name, column = match.group(1), match.group(2)
        plan = plans.get(dataset_name)
        if plan is None:
            return match.group(0)
        reference = plan.refs.get(column)
        if reference is None:
            return match.group(0)
        return "${" + f"{plan.slug}.{reference}" + "}"

    return _QUALIFIED_REF_RE.sub(replace, expression)


def _measure_reference(plan: _ViewPlan, measure_name: str) -> str:
    return "${" + f"{plan.slug}.{measure_name}" + "}"


def _build_measures(
    model: ResolvedModel,
    plans: dict[str, _ViewPlan],
    names: NameRegistry,
    dialect: OssieDialect,
    warnings: list[str],
) -> None:
    """Attach every metric to a view as a LookML measure.

    Runs in two passes so a ratio metric can reuse an already-emitted plain
    aggregate measure (e.g. `total_sales` doubling as a ratio's numerator)
    regardless of the order metrics are declared in - the same reuse the SML
    emitter does.
    """
    fallback_view = next(iter(plans.values()))
    expressions: dict[str, str] = {}
    for name, metric in model.metrics.items():
        try:
            expressions[name] = model.resolve_expression(metric.expression, dialect)
        except MissingExpressionError:
            warnings.append(
                f"LOSSY: metric {name!r} has no {dialect.value} or ANSI_SQL expression; "
                "omitted from the LookML project."
            )

    # Pass 1: plain `AGG(dataset.column)` metrics become typed measures.
    simple_measures: dict[tuple[str, str, str], tuple[_ViewPlan, str]] = {}
    handled: set[str] = set()
    for metric_name, expression in expressions.items():
        decomposed = decompose_simple_aggregate(expression)
        if decomposed is None or decomposed[1] not in plans:
            continue
        method, dataset_name, column = decomposed
        lookml_type = _LOOKML_MEASURE_TYPE.get(method)
        if lookml_type is None:
            continue  # stddev/variance/percentile/count-non-null: pass 2 handles it
        plan = plans[dataset_name]
        metric = model.metrics[metric_name]
        name = names.register(plan.slug, metric_name)
        block = Block("measure", name)
        block.add("type", Bare(lookml_type))
        block.add("description", _describe(metric.description, metric.ai_context))
        block.add("tags", _tags(metric.ai_context))
        block.add("sql", Sql("${TABLE}." + column))
        plan.measures.append(block)
        # First declaration wins: two metrics can share an aggregate shape (the
        # TPC-DS fixture's `total_sales` and `sales_by_brand` are both
        # `SUM(store_sales.ss_ext_sales_price)`), and a ratio should reuse the
        # one declared first rather than whichever happened to be seen last.
        simple_measures.setdefault((method, dataset_name, column), (plan, name))
        handled.add(metric_name)

    # Pass 2: ratios, then anything left over as a raw `type: number` measure.
    for metric_name, expression in expressions.items():
        if metric_name in handled:
            continue
        metric = model.metrics[metric_name]
        description = _describe(metric.description, metric.ai_context)
        tags = _tags(metric.ai_context)

        ratio = decompose_ratio_aggregate(expression)
        if ratio is not None and ratio.numerator[1] in plans and ratio.denominator[1] in plans:
            numerator = _component_measure(ratio.numerator, metric_name, "numerator", plans, names, simple_measures)
            denominator = _component_measure(ratio.denominator, metric_name, "denominator", plans, names, simple_measures)
            if numerator is not None and denominator is not None:
                plan = plans[ratio.numerator[1]]
                name = names.register(plan.slug, metric_name)
                block = Block("measure", name)
                block.add("type", Bare("number"))
                block.add("description", description)
                block.add("tags", tags)
                denominator_sql = _measure_reference(*denominator)
                if ratio.denominator_nullif_guard_dropped:
                    denominator_sql = f"NULLIF({denominator_sql}, 0)"
                block.add("sql", Sql(f"{_measure_reference(*numerator)} / {denominator_sql}"))
                plan.measures.append(block)
                # Dividing two *measure references* (rather than inlining raw
                # aggregate SQL) is what keeps a ratio fan-out safe: Looker
                # computes each component with symmetric aggregates and only then
                # divides. The one caveat left is reach, not correctness.
                if denominator[0] is not plan or numerator[0] is not plan:
                    views = sorted({numerator[0].slug, denominator[0].slug, plan.slug})
                    block.comments.append(
                        "Cross-view ratio: valid only in explores joining " + ", ".join(views) + "."
                    )
                    warnings.append(
                        f"metric {metric_name!r} divides measures across views "
                        f"{views}; emitted on view {plan.slug!r}, where it resolves only in "
                        "explores that join all of them. Its components are typed measures, "
                        "so symmetric aggregates still apply."
                    )
                continue

        referenced = model.referenced_datasets(expression)
        plan = plans[referenced[0]] if referenced else fallback_view
        if not referenced:
            warnings.append(
                f"metric {metric_name!r} references no known dataset; attached to view "
                f"{plan.slug!r} as a best-effort placement."
            )
        name = names.register(plan.slug, metric_name)
        block = Block("measure", name)
        block.add("type", Bare("number"))
        block.add("description", description)
        block.add("tags", tags)
        block.add("sql", Sql(_rewrite_references(expression, plans)))
        plan.measures.append(block)

        if len(referenced) > 1:
            block.comments.append(
                "Cross-view expression: valid only in explores joining "
                + ", ".join(sorted(plans[d].slug for d in referenced))
                + "."
            )
            warnings.append(
                f"metric {metric_name!r} spans datasets {referenced}; emitted on view "
                f"{plan.slug!r} as a `type: number` measure. Looker does not apply "
                "symmetric aggregates to `type: number`, so it is only correct in "
                "explores where no join above it fans out rows - review before use."
            )
        else:
            warnings.append(
                f"metric {metric_name!r} does not decompose to a plain aggregate; emitted "
                f"on view {plan.slug!r} as a `type: number` measure carrying the raw SQL, "
                "which Looker's symmetric aggregates do not protect."
            )


def _component_measure(
    component: tuple[str, str, str],
    metric_name: str,
    role: str,
    plans: dict[str, _ViewPlan],
    names: NameRegistry,
    simple_measures: dict[tuple[str, str, str], tuple[_ViewPlan, str]],
) -> tuple[_ViewPlan, str] | None:
    """Reuse an existing measure with this aggregate shape, or synthesize a hidden one."""
    existing = simple_measures.get(component)
    if existing is not None:
        return existing

    method, dataset_name, column = component
    lookml_type = _LOOKML_MEASURE_TYPE.get(method)
    if lookml_type is None:
        return None
    plan = plans[dataset_name]
    name = names.register(plan.slug, f"__{metric_name}_{role}", preferred=f"{slugify(metric_name)}_{role}")
    block = Block("measure", name)
    block.add("type", Bare(lookml_type))
    block.add("hidden", Bare("yes"))
    block.add("sql", Sql("${TABLE}." + column))
    block.comments.append(f"{role.capitalize()} of metric {metric_name!r}.")
    plan.measures.append(block)
    simple_measures[component] = (plan, name)
    return plan, name


def _explore_roots(model: ResolvedModel, warnings: list[str]) -> list[str]:
    """Datasets to root an explore at: fact tables, else metric anchors, else all."""
    from_sides = {rel.from_dataset for rel in model.relationships}
    to_sides = {rel.to for rel in model.relationships}
    roots = [name for name in model.datasets if name in from_sides and name not in to_sides]
    if roots:
        return roots

    # No dataset is purely a fact table (a relationship cycle, or every dataset is
    # some relationship's target). Fall back to the datasets metrics are anchored
    # on, which is where measures will live.
    anchors: list[str] = []
    for metric in model.metrics.values():
        for dialect_expression in metric.expression.dialects[:1]:
            referenced = model.referenced_datasets(dialect_expression.expression)
            if referenced and referenced[0] not in anchors:
                anchors.append(referenced[0])
    if anchors:
        warnings.append(
            "no dataset is purely a fact table (every dataset is the target of some "
            f"relationship), so explores were rooted at the metric anchors {anchors}."
        )
        return anchors

    warnings.append(
        "no dataset could be identified as an explore root, so one explore was emitted "
        "per dataset."
    )
    return list(model.datasets)


def _explore_block(
    root: str,
    model: ResolvedModel,
    plans: dict[str, _ViewPlan],
    warnings: list[str],
    covered: set[str],
) -> Block:
    """One explore: the root view plus every dataset reachable by following
    relationships in their declared direction, joined in BFS order so each
    `sql_on` only references views already joined above it."""
    root_plan = plans[root]
    explore = Block("explore", root_plan.slug)
    if root_plan.dataset.name != root_plan.slug:
        explore.add("label", root_plan.dataset.name)
    explore.add("description", _describe(root_plan.dataset.description, root_plan.dataset.ai_context))

    joined = {root}
    queue: deque[str] = deque([root])
    while queue:
        current = queue.popleft()
        for rel in model.relationships:
            # Traverse only in the relationship's declared direction. An Ossie
            # relationship is always many-to-one that way (`from_columns` is the
            # foreign key, `to_columns` the key it references), so an explore
            # built this way is a pure star/snowflake: fact -> dimension ->
            # sub-dimension, with no join that fans out the root's rows.
            #
            # Walking an edge backwards would reach a SECOND fact table through a
            # conformed dimension - the classic chasm trap, where the two facts
            # multiply each other's rows. The bundled retail model has exactly
            # that shape (fct_store_sales and fct_store_returns share five
            # dimensions, and its own docs say to query them separately), so each
            # fact gets its own explore instead.
            if rel.from_dataset != current or rel.to in joined or rel.to not in plans:
                continue
            target, left_columns, right_columns = rel.to, rel.from_columns, rel.to_columns

            target_plan = plans[target]
            conditions = []
            for left_column, right_column in zip(left_columns, right_columns):
                left_ref = plans[current].refs.get(left_column)
                right_ref = target_plan.refs.get(right_column)
                if left_ref is None or right_ref is None:
                    conditions = []
                    break
                conditions.append(
                    "${" + f"{plans[current].slug}.{left_ref}" + "} = "
                    "${" + f"{target_plan.slug}.{right_ref}" + "}"
                )
            if not conditions:
                warnings.append(
                    f"LOSSY: relationship {rel.name!r} references columns with no emitted "
                    f"dimension; the join to {target!r} was omitted from explore {root_plan.slug!r}."
                )
                continue

            join = Block("join", target_plan.slug)
            join.add("type", Bare("left_outer"))
            join.add("relationship", Bare("many_to_one"))
            join.add("sql_on", Sql(" AND ".join(conditions)))
            explore.children.append(join)
            joined.add(target)
            queue.append(target)

    covered.update(joined)
    return explore


def emit_lookml_project(
    model: ResolvedModel,
    *,
    connection: str = DEFAULT_CONNECTION,
    dialect: OssieDialect = OssieDialect.ANSI_SQL,
) -> LookmlEmitResult:
    """Render `model` as a LookML project: `views/*.view.lkml` + one `.model.lkml`."""
    warnings: list[str] = []
    names = NameRegistry(warnings)

    if connection == DEFAULT_CONNECTION:
        warnings.append(
            f"no Looker connection name was given, so the model file uses the placeholder "
            f"{DEFAULT_CONNECTION!r}. Ossie does not model a connection; set one before "
            "deploying this project, or Looker will fail to run any query."
        )

    plans: dict[str, _ViewPlan] = {
        name: _ViewPlan(dataset=dataset, slug=names.register("view", name))
        for name, dataset in model.datasets.items()
    }

    metric_columns = _referenced_columns(model, dialect)
    join_columns = _join_columns(model)
    for name, plan in plans.items():
        extra = set(plan.dataset.primary_key or []) | join_columns[name] | metric_columns[name]
        _build_dimensions(plan, model, names, dialect, extra, warnings)

    _build_measures(model, plans, names, dialect, warnings)

    files: dict[str, str] = {}
    for plan in plans.values():
        view = Block("view", plan.slug)
        view.add("sql_table_name", Sql(plan.dataset.source))
        view.children.extend(plan.dimensions)
        view.children.extend(plan.measures)
        header = [f"Generated by Lexis from Ossie dataset {plan.dataset.name!r}."]
        if plan.dataset.description:
            header.append(plan.dataset.description)
        files[f"views/{plan.slug}.view.lkml"] = render_file(header, [view])

    semantic_model = model.semantic_model
    model_slug = slugify(semantic_model.name)
    header_block = Block("")
    header_block.add("connection", connection)
    header_block.add("include", "/views/*.view.lkml")
    covered: set[str] = set()
    explores = [
        _explore_block(root, model, plans, warnings, covered)
        for root in _explore_roots(model, warnings)
    ]
    unreachable = sorted(set(plans) - covered)
    if unreachable:
        warnings.append(
            f"dataset(s) {unreachable} are not reachable from any explore root by "
            "following relationships in their declared direction, so their views are "
            "emitted but no explore exposes them. Add a relationship whose `from` side "
            "is the dataset holding the foreign key."
        )
    model_header = [f"Generated by Lexis from Ossie semantic model {semantic_model.name!r}."]
    if semantic_model.description:
        model_header.append(semantic_model.description)
    files[f"{model_slug}.model.lkml"] = render_file(model_header, [header_block, *explores])

    if semantic_model.custom_extensions:
        warnings.append(
            "LOSSY: semantic model custom_extensions have no LookML equivalent and were dropped."
        )

    return LookmlEmitResult(files=files, warnings=warnings)
