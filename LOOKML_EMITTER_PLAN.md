# Ossie → LookML emitter

Design plan for a `--target lookml` emitter in [Lexis](https://github.com/PuspenduBanerjee/Lexis),
written to match the conventions of the repo's existing `SML_OSSIE_CONVERTER_PLAN.md`.

## Context

Lexis already emits 9 targets from one `OssieDocument`
(`src/lexis/dispatch.py:TARGETS`): 5 SQL dialects, plus `cube`, `dbt`, `mcp`,
`snowflake_semantic_view`, and the multi-file `sml`. LookML is the obvious next
BI consumer: Looker is the last major semantic-layer destination Lexis doesn't
reach, and — unlike Cube or Snowflake semantic views — a LookML project is a
*directory* of files, which is exactly the shape `sml` already taught the CLI,
API, schema types and web UI to handle (`TranspileResult.content: str | dict[str, str]`).

That is the single most important architectural fact for scoping this work:
**the multi-file plumbing is already done.** `cli.py` writes a `dict` result to a
`--out` directory, `TranspileOut.content` is already `str | dict[str, str]`, and
`TranspileView.tsx` already renders a `FileTree` for dict results. Adding LookML
is almost entirely new-module work, not plumbing work.

Three things LookML needs that Ossie does not carry, and which drive most of the
design below:

1. **A connection name.** Every `.model.lkml` starts with `connection: "..."`.
   Ossie has no such concept (`dataset.source` is `database.schema.table`).
2. **Explores.** Ossie has a relationship *graph*; Looker needs rooted,
   explicitly-joined *trees* with a declared base view per explore.
3. **Primary keys on every view.** Looker's symmetric aggregates — the mechanism
   that keeps `SUM` correct across fan-out joins — require a `primary_key: yes`
   dimension. Ossie's `primary_key` is optional and may be composite; LookML
   allows exactly one primary-key dimension per view.

## Data model mapping reference

### Object/concept mapping

| Ossie | LookML | Notes |
|---|---|---|
| `OssieSemanticModel` | one `<name>.model.lkml` + N `views/<dataset>.view.lkml` | project root |
| `OssieDataset` | `view: <name> { sql_table_name: <source> ;; }` | `source` is already `db.schema.table` |
| `OssieDataset.description` | `description:` on the view's explore + `# ` comment | LookML views have no `description` parameter; explores do |
| `OssieDataset.primary_key` (1 col) | `primary_key: yes` on that dimension | |
| `OssieDataset.primary_key` (N cols) | synthesized hidden concat dimension | see Edge case 2 |
| `OssieDataset.unique_keys` | — | no LookML slot; dropped with a `LOSSY:` warning |
| `OssieField` (non-temporal) | `dimension: <name> { type: … sql: ${TABLE}.<expr> ;; }` | |
| `OssieField` (temporal `datatype`) | `dimension_group: <stem> { type: time timeframes: […] }` | see Edge case 5 |
| `OssieField.label` / `.description` | `label:` / `description:` | escaped (Edge case 8) |
| `OssieField.ai_context.synonyms` | `tags: ["…", …]` | LookML's only free-form field vocabulary slot |
| `OssieField.ai_context.instructions` | appended to `description` | |
| `OssieField.ai_context.examples` | — | dropped with a warning |
| `OssieRelationship` | `join:` inside an explore | `type: left_outer`, `sql_on: ${a.c} = ${b.c} ;;` |
| `OssieMetric` (simple aggregate) | `measure: { type: sum\|average\|min\|max\|count_distinct }` | typed, symmetric-aggregate-safe |
| `OssieMetric` (ratio of aggregates) | two component `measure:`s + a `type: number` measure | reuses `decompose_ratio_aggregate` |
| `OssieMetric` (anything else) | `measure: { type: number sql: <rewritten> ;; }` | fan-out warning (Edge case 4) |
| `custom_extensions` | — | out of scope for v1 |

### Datatype mapping

| `OssieDataType` | LookML `type` |
|---|---|
| `String`, `Opaque` | `string` |
| `Integer`, `Decimal`, `Float` | `number` |
| `Boolean` | `yesno` |
| `Date` | `dimension_group` / `type: time`, `timeframes: [raw, date, week, month, quarter, year]` |
| `DateTime`, `DateTimeTz` | `dimension_group` / `type: time`, `timeframes: [raw, time, date, week, month, quarter, year]` |
| `Time` | `string` — LookML has no time-of-day dimension type; warn |
| *(absent)* | `string` (LookML's own default) |

### Measure typing (extends the existing decomposition prior art)

`src/lexis/sml/_common.py` already has `decompose_simple_aggregate()` and
`decompose_ratio_aggregate()` — battle-tested against the same fixture set. The
LookML emitter reuses both rather than growing a third regex table, mapping SML's
`calculation_method` vocabulary onto LookML measure types:

| Decomposed method | LookML |
|---|---|
| `sum` | `type: sum`, `sql: ${TABLE}.<col> ;;` |
| `average` | `type: average` |
| `minimum` / `maximum` | `type: min` / `type: max` |
| `count distinct` | `type: count_distinct` |
| `count non-null` | `type: number`, `sql: COUNT(${TABLE}.<col>) ;;` — LookML's `type: count` is a *row* count and takes no `sql` |
| `stddev_*`, `var_*`, `percentile`, `count_if` | `type: number` with the raw aggregate |
| ratio | `type: number`, `sql: ${num_measure} / NULLIF(${den_measure}, 0) ;;` |
| *(no match)* | `type: number` with the reference-rewritten raw SQL |

Reference rewriting is the one genuinely new piece of expression handling:
Ossie writes `SUM(store_sales.ss_ext_sales_price)`, LookML wants
`SUM(${store_sales.ss_ext_sales_price})`. The rewriter walks
`ResolvedModel.referenced_datasets()`-style `dataset.column` tokens and wraps each
one that resolves to a real field (or a synthesized hidden dimension) in `${…}`,
leaving everything else — function names, literals, operators — untouched.

## Architecture

```
src/lexis/transpilers/lookml/
    __init__.py      re-exports emit_lookml_project
    writer.py        LookML serializer: blocks, `;;` terminators, escaping, indentation
    naming.py        identifier slugification + collision registry
    emit.py          the mapping itself: views, explores, measures, warnings
```

Emit-only, so it lives under `transpilers/` like `cube.py`/`dbt_ossie.py` — not
at `src/lexis/` top level like `sml/`, which earned its own package by being
bidirectional. If Phase 4 (LookML → Ossie) happens, promote the package to
`src/lexis/lookml/` then, matching `sml/`'s layout.

**No new runtime dependency.** `writer.py` hand-rolls the ~120 lines of LookML
serialization, the same way `snowflake_semantic_view.py` hand-rolls DDL. The
excellent [`lkml`](https://pypi.org/project/lkml/) parser (1.3.7) goes in
`[project.optional-dependencies].dev` instead, where tests use it as an
*independent* parser to prove the output is well-formed — the same trick
`test_cube_emitter.py` plays with `yaml.safe_load`. Using someone else's parser to
check your own writer is worth far more than using their writer.

### Entry point

```python
@dataclass(frozen=True)
class LookmlEmitResult:
    files: dict[str, str]     # "views/store_sales.view.lkml" -> content
    warnings: list[str]

def emit_lookml_project(
    model: ResolvedModel,
    *,
    connection: str = "lexis_connection",
    dialect: OssieDialect = OssieDialect.ANSI_SQL,
) -> LookmlEmitResult: ...
```

### Options plumbing (the one cross-cutting change)

`connection` and `dialect` are the first per-target options Lexis has needed, so
`dispatch.transpile()` grows one backwards-compatible keyword:

```python
def transpile(document, model, target, metric=None, group_by=None,
              options: Mapping[str, str] | None = None) -> TranspileResult:
```

Unknown keys raise `ValueError` (surfaced as a `click.UsageError` by the CLI and
a 422 by the API, matching how the missing-metric error already flows). CLI gets
two discoverable flags — `--lookml-connection NAME`, `--lookml-dialect DIALECT` —
rather than a generic `--option k=v`, and `TranspileIn` grows
`options: dict[str, str] | None = None`.

### Explore generation

Ossie's relationship graph has no notion of a root. The algorithm:

1. **Candidate roots** = datasets that appear as `from` in ≥1 relationship and
   never as `to` — i.e. fact tables. For the TPC-DS fixture that is exactly
   `store_sales`.
2. If that set is empty (a model with no relationships, or a cycle), fall back to
   every dataset that is the first referenced dataset of ≥1 metric; failing that,
   every dataset.
3. For each root, BFS the relationship graph and emit one `join:` per edge **in
   BFS order**, so every `sql_on` references a view already joined above it — a
   hard Looker requirement, and exactly the traversal
   `SqlDialectEmitter._from_and_joins()` already performs.
4. **Follow relationships only in their declared direction** (`from → to`), so
   every join is `relationship: many_to_one`. An Ossie relationship is always
   many-to-one that way — `from_columns` is the foreign key, `to_columns` the key
   it references — so an explore built this way is a pure star/snowflake with no
   join that fans out the root.

   Walking an edge backwards would reach a *second fact table* through a
   conformed dimension: the classic chasm trap, where the two facts multiply each
   other's rows. The bundled `retail_analytics` model has exactly that shape —
   `fct_store_sales` and `fct_store_returns` share five dimensions, and its own
   documentation says to query them separately — so each fact gets its own
   explore instead. A dataset no explore can reach this way is still emitted as a
   view, and warned about rather than silently dropped.
5. Join type is always `left_outer`. Ossie models FK presence, not enforcement,
   and an inner join would silently drop fact rows with a null FK.

Explores live in the model file, per Looker convention.

## Worked example (the repo's own TPC-DS fixture)

`lexis transpile tests/fixtures/tpcds_semantic_model.yaml --target lookml \
  --lookml-connection tpcds_snowflake --out ./lookml_project`

`lookml_project/views/store_sales.view.lkml`:

```lkml
# Generated by Lexis from Ossie semantic model "tpcds_retail_model".
# Fact table containing all store sales transactions

view: store_sales {
  sql_table_name: tpcds.public.store_sales ;;

  # Ossie primary_key is composite [ss_item_sk, ss_ticket_number]; LookML allows
  # only one primary_key dimension, so it is synthesized as a concatenation.
  dimension: lexis_primary_key {
    primary_key: yes
    hidden: yes
    sql: CONCAT(${TABLE}.ss_item_sk, '-', ${TABLE}.ss_ticket_number) ;;
  }

  # Referenced by store_sales.lexis_primary_key but not declared as an Ossie field.
  dimension: ss_ticket_number {
    hidden: yes
    sql: ${TABLE}.ss_ticket_number ;;
  }

  dimension: ss_sold_date_sk {
    description: "Foreign key to date dimension"
    tags: ["sale date", "transaction date"]
    sql: ${TABLE}.ss_sold_date_sk ;;
  }

  dimension: ss_ext_sales_price {
    type: number
    description: "Extended sales price (quantity * price)"
    tags: ["total price", "line total"]
    sql: ${TABLE}.ss_ext_sales_price ;;
  }

  measure: total_sales {
    type: sum
    description: "Total sales revenue across all transactions"
    tags: ["total revenue", "gross sales", "sales amount"]
    sql: ${TABLE}.ss_ext_sales_price ;;
  }

  # Cross-view expression: correct only in explores where `customer` is joined,
  # and NOT protected by Looker's symmetric aggregates.
  measure: customer_lifetime_value {
    type: number
    description: "Average lifetime sales value per customer"
    sql: SUM(${store_sales.ss_ext_sales_price}) / COUNT(DISTINCT ${customer.c_customer_sk}) ;;
  }
}
```

`lookml_project/tpcds_retail_model.model.lkml`:

```lkml
connection: "tpcds_snowflake"

include: "/views/*.view.lkml"

explore: store_sales {
  description: "TPC-DS retail semantic model for sales and customer analytics"

  join: date_dim {
    type: left_outer
    relationship: many_to_one
    sql_on: ${store_sales.ss_sold_date_sk} = ${date_dim.d_date_sk} ;;
  }

  join: customer {
    type: left_outer
    relationship: many_to_one
    sql_on: ${store_sales.ss_customer_sk} = ${customer.c_customer_sk} ;;
  }
  # … item, store
}
```

Note what the fixture already exercises: a **composite PK whose second column
(`ss_ticket_number`) is not declared as a field**, a **`store` dataset whose
`primary_key` (`s_store_sk`) differs from its `unique_keys` (`s_store_id`)**, a
**computed multi-column field** (`customer_full_name`), **three `is_time: true`
fields that are not temporal** (`d_year`, `d_quarter_name`, `d_month_name`), a
**cross-dataset metric** (`customer_lifetime_value`) and a **`NULLIF`-guarded
ratio** (`store_productivity`). Every edge case below is reachable from the
bundled fixture — no synthetic test model needed for the core suite.

## Implementation phases

### Phase 1 — core emitter (the bulk of the work)

- `writer.py`: block/parameter serialization, `;;` termination, string escaping,
  2-space indentation, deterministic ordering.
- `naming.py`: slugify to `[a-z_][a-z0-9_]*`, reserved-word check, collision
  registry with `_2` suffixing and a warning per collision.
- `emit.py`: views (dimensions, dimension groups, synthesized hidden
  join/PK dimensions, measures), explores, the model file, and the warning set.
- Wire it up: `dispatch.py` (`TARGETS`, the `options` keyword, the branch),
  `cli.py` (two flags), `schemas.py` (`TARGET` literal, `TranspileIn.options`),
  `routers/transpile.py` (pass options through),
  `frontend/src/api/types.ts` (`Target` union, `ALL_TARGETS`).
- Tests: `tests/test_lookml_emitter.py` (see Verification).

### Phase 2 — fidelity and ergonomics

- `ai_context` → `tags:` / description folding; `label:` from `OssieField.label`.
- Golden-file fixtures for the TPC-DS and retail models.
- `TranspileView.tsx`: a connection-name input shown only when
  `target === "lookml"` (the first target-specific control in that component —
  keep it to one conditional `<label>`, mirroring the existing `isSql` branch).
- README: target list, the `lookml` example, `Project structure` entry.

### Phase 3 — Looker-validated output

- Load the emitted project into a Looker dev branch, run the LookML validator and
  (optionally) `spectacles` SQL validation against a real warehouse; fix whatever
  the validator finds. This cannot run in CI and is a manual gate — document the
  procedure in the README alongside the existing MCP connection walkthroughs.

### Phase 4 — optional, LookML → Ossie

Follows the `sml` precedent exactly: `lkml` becomes a real (optional) dependency,
`lexis import lookml <project_dir>` joins `lexis import sml`, and unrepresentable
LookML lands in the `custom_extensions` stash under `vendor_name="LOOKML"` per
`sml/_common.py`'s protocol, so a round trip does not silently lose structure.
Explicitly out of scope until Phases 1–3 land.

## Edge cases and challenges that cannot be fully resolved

1. **Connection name is not in Ossie.** Defaults to `lexis_connection` with a
   warning telling the user to set `--lookml-connection`. The emitted project
   will not run in Looker until it matches a real connection.

2. **Composite primary keys.** LookML permits one `primary_key: yes` dimension.
   Emitted as a hidden `CONCAT(col, '-', col)` dimension named
   `lexis_primary_key`, with a `LOSSY:` warning. `CONCAT` is not universal
   (`||` on Postgres/DuckDB/Snowflake) — the concatenation operator follows the
   selected `--lookml-dialect`.

3. **No primary key at all.** No `primary_key` dimension is emitted and a
   warning is raised: Looker's symmetric aggregates cannot protect that view, so
   any measure on it is wrong the moment a one-to-many join fans out rows. This
   is the single most dangerous silent-failure mode in the whole target, and it is
   worth failing loudly about rather than politely.

4. **Non-decomposable metrics.** They become `type: number` measures carrying raw
   SQL. Looker does not apply symmetric aggregates to `type: number`, so such a
   measure is only correct in explores with no fan-out above it. Warned per
   metric. Same best-effort placement convention the Cube and
   Snowflake-semantic-view emitters already use: attach to the metric's first
   referenced dataset.

   A **ratio** metric is the exception, and deliberately carries a different
   warning: it divides two *typed measure references* (`${a.num} / ${b.den}`), so
   Looker computes each component with symmetric aggregates and only then
   divides. Its caveat is reach rather than correctness — it resolves only in
   explores joining both views.

5. **`dimension_group` renaming.** Looker appends a timeframe suffix to a
   dimension group's fields: group `created` yields `created_date`,
   `created_month`, …. A field literally named `d_date` therefore cannot stay
   `d_date` (its group would emit `d_date_date`). Rule: strip a trailing
   `_date`/`_at`/`_time`/`_timestamp` from the group name, so `d_date` → group
   `d` → field `d_date`, which is what a Looker developer would hand-write. If
   stripping yields an empty or colliding name, use `<field>_group` and warn.
   **Any join or measure referencing that field must use the suffixed name** —
   the reference rewriter consults the naming registry, not the Ossie name.

6. **`is_time: true` on a non-temporal field.** The TPC-DS fixture marks `d_year`
   (an integer) and `d_month_name` (a string) as time dimensions. Emitting a
   `type: time` dimension group over those produces SQL Looker cannot run. Rule:
   a `dimension_group` is emitted **only** when `datatype` is temporal, never from
   `is_time` alone; a mismatch emits a plain dimension plus a warning. Note this
   deliberately diverges from `OssieField.is_time_dimension()`, which the Cube
   emitter trusts — because Cube's `type: time` is more forgiving than Looker's.

7. **Unqualified multi-column expressions.** `c_first_name || ' ' || c_last_name`
   cannot be reliably rewritten to `${TABLE}.c_first_name || ' ' || ${TABLE}.c_last_name`
   without a real SQL parser. Following `SqlDialectEmitter._resolve_field_expr()`:
   a bare identifier is qualified with `${TABLE}.`, anything else is passed through
   verbatim with a warning. Unqualified columns resolve fine in a single-table
   view but can be ambiguous once joined.

8. **Escaping.** LookML strings are double-quoted with backslash escapes and no
   multi-line support; `sql:` blocks are raw until `;;`. The writer escapes `"`
   and `\` in quoted values, collapses newlines in descriptions to spaces, and
   rejects (rather than silently truncates) a `sql` expression containing `;;`.

9. **Identifier collisions.** Ossie names are free-form; LookML identifiers are
   `[a-z_][a-z0-9_]*`. Two datasets named `Store Sales` and `store_sales` both
   slugify to `store_sales`. The naming registry suffixes `_2` and warns —
   never silently overwrites.

10. **Only the first semantic model is converted.** `cli.py` and the API both do
    `document.semantic_model[0]` today. The LookML emitter inherits that; a
    document with several models warns that the rest were skipped.

11. **`unique_keys`, `examples`, `custom_extensions`** have no LookML home and are
    dropped with warnings, per the repo's existing "no silent loss" convention.

## Verification

1. **Independent-parser round trip.** Every emitted file is parsed with `lkml`
   (dev dependency) in tests; a parse failure is a test failure. This is the
   structural guarantee the hand-rolled writer needs.
2. **Structural assertions** on the parsed dicts, mirroring
   `test_cube_emitter.py`: view set equals dataset set, every view has
   `sql_table_name`, the `store_sales` explore joins exactly the four dimension
   views, each join's `relationship` is `many_to_one`.
3. **Semantic cross-check.** For every metric in the fixture, assert the emitted
   measure's `type`/`sql` agrees with `decompose_simple_aggregate()` /
   `decompose_ratio_aggregate()` on the Ossie expression — so the two decomposition
   paths can never drift apart.
4. **Golden files** under `tests/fixtures/lookml/expected/` compared exactly, to
   catch formatting regressions the parser would happily accept.
5. **Warning-content tests**, asserting the `LOSSY:` prefix convention from
   `sml/emit.py` and that each edge case above produces its warning.
6. **Route + CLI tests**: one case in `tests/api/test_transpile_routes.py`
   (it returns a dict, not a string) and one in `tests/test_cli.py` proving
   `--out <dir>` writes the file tree.
7. **Manual Looker validation** (Phase 3) — the only check that proves the output
   actually works, and the only one that cannot be automated here.

## Effort estimate

| Phase | Scope | Rough size |
|---|---|---|
| 1 | writer + naming + emit + wiring + core tests | ~700 LOC src, ~300 LOC tests |
| 2 | ai_context, golden files, UI input, README | ~150 LOC + docs |
| 3 | manual Looker validation + fixes | 1 session against a real instance |
| 4 | LookML → Ossie import (optional) | comparable to `sml/parse.py` (~550 LOC) |
