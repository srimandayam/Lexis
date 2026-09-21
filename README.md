# Lexis

An open, [Apache Ossie](https://github.com/apache/ossie)-native semantic layer:
author a data model once in Ossie YAML, then transpile it to warehouse-native SQL
(Snowflake, BigQuery, Databricks, DuckDB, Postgres) and to formats BI/AI consumers
understand (Cube.js schema, dbt-core Ossie documents, MCP tool manifests grounded in
`ai_context`).

Two ways to use it: a `lexis` CLI/library, and a web UI (FastAPI + React) with a
persisted multi-model workspace, role-based access, and live semantic query execution.

See [docs/architecture-plan.md](docs/architecture-plan.md) for the full architecture
writeup and design rationale.

This repo tracks the upstream [Ossie spec](https://github.com/apache/ossie)
as a git submodule at `third_party/ossie` (schema, converters docs, examples) so our
vendored model classes (`src/lexis/_vendor/ossie/`) can be kept in sync with it -
see "Keeping Ossie in sync" below.

## Quickstart: Install from PyPI

```bash
pip install lexis-cli
```

Save this as `model.yaml`:

```yaml
version: "0.2.0.dev0"
semantic_model:
  - name: shop
    datasets:
      - name: orders
        source: analytics.public.orders
        fields:
          - name: amount
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: amount
    metrics:
      - name: total_revenue
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(orders.amount)
```

Then transpile it:

```bash
lexis transpile model.yaml --target duckdb --metric total_revenue
```

```sql
SELECT SUM(orders.amount) AS "total_revenue"
FROM analytics.public.orders AS "orders"
```

Same command works for every target below — see [Quickstart: CLI (from source)](#quickstart-cli-from-source) for the full target list and the repo's own TPC-DS-based example fixture.

## Quickstart: CLI (from source)

Requires Python 3.11+.

```bash
git submodule update --init   # first time only, or after a fresh clone
pip install -e .
lexis transpile tests/fixtures/tpcds_semantic_model.yaml --target duckdb --metric total_sales
```

```sql
SELECT SUM(store_sales.ss_ext_sales_price) AS "total_sales"
FROM tpcds.public.store_sales AS "store_sales"
```

Other targets: `postgres`, `bigquery`, `databricks`, `snowflake` (all take `--metric`,
and an optional repeatable `--group-by dataset.field`), plus `cube`, `dbt`, `mcp`,
`snowflake_semantic_view`, `sml`, and `lookml` (whole-model outputs, no `--metric`
needed):

```bash
lexis transpile tests/fixtures/tpcds_semantic_model.yaml --target mcp
lexis transpile tests/fixtures/tpcds_semantic_model.yaml \
  --target duckdb --metric customer_lifetime_value --group-by item.i_category
```

`snowflake_semantic_view` emits a `CREATE OR REPLACE SEMANTIC VIEW` DDL statement
(Snowflake's native Cortex Analyst semantic view) with `TABLES`/`RELATIONSHIPS`/
`FACTS`/`DIMENSIONS`/`METRICS` clauses built from the model's datasets, relationships,
fields, and metrics — fields with a `dimension` block become `DIMENSIONS`, fields
without one become `FACTS`, and `ai_context` synonyms/descriptions map to `WITH
SYNONYMS`/`COMMENT`:

```bash
lexis transpile tests/fixtures/tpcds_semantic_model.yaml --target snowflake_semantic_view
```

`lookml` emits a whole Looker project — one `views/<dataset>.view.lkml` per dataset
plus a `<model>.model.lkml` carrying the connection, `include`, and explores. Ossie
fields become dimensions (temporal ones become `dimension_group`s), metrics become
typed measures where they decompose to a plain aggregate, and each fact table gets its
own explore joining its dimensions `many_to_one`:

```bash
lexis transpile tests/fixtures/tpcds_semantic_model.yaml \
  --target lookml --lookml-connection my_warehouse --out ./lookml_project
```

Ossie doesn't model a Looker connection, so `--lookml-connection` sets the name the
model file declares; without it the project is emitted with a placeholder and a warning.
`--lookml-dialect` (default `ANSI_SQL`) picks which dialect's expressions get embedded.
The emitter warns wherever LookML can't represent something faithfully — composite
primary keys, metrics that don't decompose to a plain aggregate, `unique_keys` — so
review the warnings before deploying a generated project.

Add `--out <file>` to write to a file instead of stdout (or, for the multi-file `sml`
and `lookml` targets, a directory).

A bundled demo dataset (the same data the web UI's "Demo dataset" run mode uses
in-memory) can be exported to a real `.duckdb` file, handy as a seed file for the
Upload run mode or a `duckdb_file` connection:

```bash
pip install -e ".[mcp]"        # needs the optional duckdb dependency
lexis export-demo-dataset --out demo.duckdb                       # small TPC-DS fixture (default)
lexis export-demo-dataset --dataset retail --out retail.duckdb    # 10,000-fact retail analytics dataset
```

Pass `--force` to overwrite an existing file at `--out`. See
[The bundled demo datasets](#the-bundled-demo-datasets) below for what each one contains.

## The bundled demo datasets

Lexis ships two in-memory demo datasets so any model can be run for real without a
warehouse. The web UI's and API's **"Demo dataset"** run mode picks whichever one
matches the model's `source` catalog automatically; the CLI (`export-demo-dataset`)
selects it with `--dataset`.

| `--dataset` | Bundled model | Catalog | Size | Best for |
|---|---|---|---|---|
| `tpcds` *(default)* | `tpcds_retail_model` — [`tests/fixtures/tpcds_semantic_model.yaml`](tests/fixtures/tpcds_semantic_model.yaml) | `tpcds.public.*` | 7 `store_sales` rows, 2 items, 2 customers, 6 dates | small deterministic examples, emitter/JOIN behaviour |
| `retail` | `retail_analytics` — [`src/lexis_api/sample_data/retail_analytics_model.yaml`](src/lexis_api/sample_data/retail_analytics_model.yaml) | `retail.public.*` | **10,000 sales facts** + 1,500 returns, 800 customers, 300 items, 25 stores, 40 promotions, 3 years of dates | realistic analytics — segmentation, seasonality, basket/AOV, promo lift, returns |

### The `retail` dataset

A multi-fact star schema: two fact tables sharing five conformed dimensions, all
generated from a fixed seed ([`src/lexis/retail_demo_data.py`](src/lexis/retail_demo_data.py)),
so every number is reproducible.

| Table | Rows | Notable columns |
|---|---|---|
| `fct_store_sales` | 10,000 (one per sold line item; a basket shares `ss_ticket_number`) | `ss_ext_sales_price`, `ss_quantity`, `ss_net_paid`, `ss_ext_wholesale_cost`, `ss_ext_discount_amt` |
| `fct_store_returns` | 1,500 | `sr_return_amt`, `sr_return_quantity`, `sr_net_loss`, `sr_reason` |
| `dim_date` | 1,096 (2022-01-01 … 2024-12-31) | `d_date_sk` = `YYYYMMDD`, `d_year`, `d_quarter_name`, `d_month_name`, `d_is_weekend`, `d_holiday_name` |
| `dim_customer` | 800 | `c_gender`, `c_age_band`, `c_income_band`, `c_education_status`, `c_loyalty_tier`, `c_preferred_channel`, `c_state` |
| `dim_item` | 300 | `i_category` (10) → `i_class` (6 each) → `i_brand` (40), `i_manufacturer`, `i_color`, `i_size` |
| `dim_store` | 25 | `s_store_type` (Flagship/Standard/Express/Outlet), `s_number_employees`, `s_floor_space`, `s_division_name` |
| `dim_promotion` | 41 | `p_channel`, `p_discount_pct`; `p_promo_sk = 0` is the "No Promotion" member |

Sales are deliberately skewed toward recent years, Q4, and weekends, so time-series
and seasonality queries show a real shape. The `retail_analytics` model exposes 17
metrics over it (`total_revenue`, `gross_margin_pct`, `units_sold`,
`transaction_count`, `avg_basket_value`, `distinct_customers`, `discount_rate_pct`,
`sales_per_employee`, `return_amount`, `net_loss_from_returns`, …). No single metric
spans both fact tables — query sales and returns separately.

**From the CLI:**

```bash
pip install -e ".[mcp]"        # needs the optional duckdb dependency

# transpile one metric, grouped by a dimension attribute
lexis transpile src/lexis_api/sample_data/retail_analytics_model.yaml \
  --target duckdb --metric total_revenue --group-by dim_item.i_category

# export the data to a real .duckdb file (reuse it in Upload mode or a duckdb_file connection)
lexis export-demo-dataset --dataset retail --out retail-demo.duckdb

# serve the retail model's 17 metrics as live MCP tools against this data
lexis mcp-serve src/lexis_api/sample_data/retail_analytics_model.yaml --demo
```

**In the web UI:** `retail_analytics` is preloaded on first run. Open it → **Test
Metrics** tab → keep **Demo dataset** mode → pick a metric (optionally "Group by" an
item / store / customer / promotion / date attribute) and **Run**, or switch to
**Time series** for a year → quarter → month → day drill-down on `dim_date.d_date`.
The **Export demo dataset (.duckdb)** button downloads exactly this data.

**Over the remote MCP endpoint:** the mounted `/mcp` endpoint has no demo mode, so
register the exported file as a connection first, then point a client at the retail
model's id:

```bash
lexis export-demo-dataset --dataset retail --out /tmp/retail-demo.duckdb --force
curl -X POST http://localhost:8000/api/connections \
  -H "X-Account-Id: 2" -H "Content-Type: application/json" \
  -d '{"name":"retail-demo","type":"duckdb_file","config":{"path":"/tmp/retail-demo.duckdb"}}'
# -> use the returned "id" as connection_id on /api/models/<retail-model-id>/mcp
```

## Quickstart: Web UI

Two servers: a FastAPI backend and a Vite/React frontend.

**Backend** (from the repo root):

```bash
pip install -e ".[dev,api]"
alembic upgrade head        # creates lexis_dev.db and its schema
uvicorn lexis_api.main:app --reload --port 8000
```

Startup automatically seeds 3 demo users (`admin`, `editor1`, `viewer1` — ids 1/2/3,
roles Admin/Editor/Viewer). Locally there's no login screen: requests are attributed to
a user via an `X-Account-Id` header (defaults to `1`/admin if omitted), driven by the
"Acting as" switcher in the UI header — a deliberate dev-only stub (`lexis_api/deps.py`).

Real sign-in is Google-based, but happens entirely at the edge rather than inside this
app: put a tunnel with Google-backed edge auth in front of it (see "Before you expose
it" below - both ngrok's `oauth` traffic-policy action and Cloudflare Access in front of
a `cloudflared` tunnel work) and it authenticates the browser against Google, then
forwards each request with an identity header - `X-User-Email` (+ `X-User-Name`) for
ngrok, `Cf-Access-Authenticated-User-Email` for Cloudflare. `deps.get_current_user`
treats either header as authoritative whenever it's present — auto-provisioning a
`viewer`-role user the first time it sees a given Google account — and it always wins
over the `X-Account-Id` stub, so the two coexist: the dev stub for local iteration,
Google auth for anything reachable by someone else. Once a deployment is *only* ever
reached through such a tunnel, set `LEXIS_DEV_AUTH_HEADER_ENABLED=0` (env var) to turn
the spoofable `X-Account-Id` stub off entirely — see `lexis_api/config.py`.

**Frontend** (in a second terminal):

```bash
cd frontend
npm install
npm run dev                 # http://localhost:5173, proxies /api -> :8000
```

Or run both together with one script, from the repo root (needs `uvicorn`/`alembic`
on PATH already, e.g. via the pyenv/venv `pip install -e ".[dev,api]"` above):

```bash
./scripts/dev.sh start      # runs migrations, launches both, backgrounded
./scripts/dev.sh status     # is either running, and which pid
./scripts/dev.sh stop       # stops both (and their child processes)
./scripts/dev.sh restart
```

Set `LEXIS_DEV_SETUP_DEMO=1` (env var, honoured by the backend however it's
launched) to skip the manual `curl` above: on startup the API writes the bundled
demo datasets to `tpcds-demo.duckdb` / `retail-demo.duckdb` in the system temp dir
(`/tmp` on Linux) and registers a `duckdb_file` connection for each (`tpcds-demo`,
`retail-demo`), so the MCP endpoint and Run tab work immediately. Both steps are
idempotent and self-heal after a reboot clears the temp dir. Relocate the files
with `LEXIS_DEMO_DATA_DIR`. Leave the flag off in production.

`./scripts/demo-dev.sh <start|stop|restart|status>` is a wrapper that runs
`dev.sh` with `LEXIS_DEV_SETUP_DEMO=1` and `LEXIS_DEV_UI_PROXY=1` (single-port:
the backend also serves the UI) preset — one command for a demo/tunnel setup.

Logs go to `.dev/api.log` / `.dev/web.log`; override ports with `LEXIS_API_PORT`/
`LEXIS_WEB_PORT` env vars. The API writes one line per request to its log
(`METHOD /path -> status`), including the `X-User-Email` / `X-User-Id` /
`X-User-Name` headers — if a tunnel's OAuth traffic policy injects them from the
authenticated identity, they show up here; `-` otherwise.

Open `http://localhost:5173`, use the "Acting as" switcher in the header to pick a
role, paste an Ossie YAML document (e.g. `tests/fixtures/tpcds_semantic_model.yaml`) to
create a model, then use the **Browse** / **Design** / **Transpile** / **Test Metrics**
tabs on the model's page (two sample models are preloaded automatically on first run,
so there's already something to open: `tpcds_retail_model` on the small TPC-DS
fixture, and `retail_analytics`, a larger multi-fact star schema backed by a
10,000-fact generated demo dataset). "Design" is a node-graph canvas (owner/admin only)
for visually adding/editing datasets, fields, and relationships — drag between the
dots on a dataset box to draw a relationship. Metrics appear as their own node,
connected by dashed edges to every dataset their expression references (a "Show
metrics" toggle hides them); "+ Add metric" creates one, and clicking a metric opens
a panel to edit its expression/description or delete it (name is fixed after
creation, like a dataset's) — owner/admin only, same as the rest of the Design tab. A
metric's panel also shows a live **time-series preview** (against the demo dataset,
reflecting the last *saved* version) when the model has any field marked
`dimension.is_time: true` — click a row to drill into the next finer grain (year →
quarter → month → day), or "Roll up" to go back.
The canvas preserves anything it has no control for (`ai_context`, `custom_extensions`,
non-ANSI_SQL dialect expressions) by merging onto the existing
parsed model rather than regenerating YAML from scratch; see
`src/lexis_api/graph_edit.py`. "Test Metrics" executes the generated SQL for real,
against the model's bundled demo dataset (TPC-DS or retail analytics, chosen from the
model's source catalog), an uploaded `.duckdb`/`.db` file, or a saved
connection (see "Connecting to Snowflake or an external DuckDB file" below) — pick
"Time series" there for the full drill-down/roll-up view (with metric, time-field, and
starting-grain pickers), or "Metric query" for the original metric+group-by mode. In
"Demo dataset" mode, an "Export demo dataset (.duckdb)" button downloads that same
data as a real file - the CLI equivalent of `lexis export-demo-dataset` above.

## Quickstart: Docker or Podman

Two images: `lexis-api` (FastAPI backend, migrations run automatically on
container start) and `lexis-web` (the built SPA served by nginx, which also
reverse-proxies `/api/*` to the backend - same same-origin-`/api` pattern the Vite
dev proxy uses, just in production).

```bash
docker compose up -d --build
```

Open `http://localhost:8000` (the `web` container publishes nginx's port 8080 on
host `8000` - see `docker-compose.yml`). The SQLite database lives on a named
volume (`lexis-data`, mounted at `/data` in the API container), so it survives
`docker compose down`/`up` and container restarts - only `docker compose down -v`
removes it. Override `LEXIS_CORS_ORIGINS`/`LEXIS_MAX_DUCKDB_UPLOAD_MB`/etc.
(see `src/lexis_api/config.py`) via `environment:` in `docker-compose.yml` if
needed - `LEXIS_CORS_ORIGINS` must list the origin you open in the browser, so
change it too if you remap the published port; if you raise the upload cap, also
raise nginx's `client_max_body_size` in `docker/nginx.conf` to match.

nginx re-resolves the `api` service at runtime (a `resolver` generated from the
container's DNS config at start, see `docker/nginx-resolver.sh`), so recreating
just the API container - `compose up -d --force-recreate api`, which gives it a
new IP - no longer 502s the frontend until `web` is restarted too.

**Demo data + connections:** layer `docker-compose.demo.yml` on top to set
`LEXIS_DEV_SETUP_DEMO=1` — the API then writes the bundled demo datasets and
registers a `duckdb_file` connection for each (`tpcds-demo`, `retail-demo`) on
startup, so the MCP endpoint / Run tab work with no manual `curl`. The `.duckdb`
files go to `/data/demo` on the `lexis-data` volume (kept across restarts).

```bash
podman compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
# docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

**Prebuilt images:** `docker-compose-dockerhub.yml` / `docker-compose-ghcr.yml`
run the published images instead of building locally — `.github/workflows/publish-images.yml`
builds once per `vX.Y.Z` tag and pushes the same image to both
`docker.io/puspendubanerjee/lexis-{api,web}` and `ghcr.io/puspendubanerjee/lexis-{api,web}`:

```bash
podman compose -f docker-compose-dockerhub.yml up -d                              # Docker Hub
podman compose -f docker-compose-ghcr.yml up -d                                   # GHCR
LEXIS_IMAGE_TAG=0.2.0 podman compose -f docker-compose-ghcr.yml up -d             # pin a release
podman compose -f docker-compose-ghcr.yml -f docker-compose.demo.yml up -d        # + demo data
```

To build the images without compose (e.g. for pushing to a registry):

```bash
./scripts/docker-build.sh                # lexis-api + lexis-web, tag :latest
./scripts/docker-build.sh 0.3.0          # ... tagged :0.3.0
./scripts/docker-build.sh --type uber    # the single-container lexis-uber image instead
./scripts/docker-build.sh --type all     # all three
./scripts/docker-build.sh --help         # full usage
```

Two more options, combinable with any of the above:

- **`--platform PLATFORM`** — cross-build for another architecture, e.g.
  `--platform linux/arm64` (needs cross-arch emulation registered - see `--help` for
  the one-time setup per engine). When you don't also pass an explicit tag, building a
  single `linux/<arch>` platform this way tags the image `<arch>-latest` instead of
  plain `latest` (`linux/arm64` → `arm64-latest`), so an arm64 and an amd64 build of
  the same version don't overwrite each other's `:latest`.
- **`--prefix PREFIX`** (or the `IMAGE_PREFIX` env var) — prepend a registry
  namespace to every image name, so the result can be `docker push`ed straight to a
  registry with no separate retag step, e.g. `--prefix puspendubanerjee/` builds
  `puspendubanerjee/lexis-api` instead of `lexis-api` (include the trailing `/`
  yourself; works for any registry, e.g. `--prefix ghcr.io/you/`).

```bash
./scripts/docker-build.sh --platform linux/arm64 --type uber
# -> lexis-uber:arm64-latest

./scripts/docker-build.sh --prefix puspendubanerjee/ --type all
# -> puspendubanerjee/lexis-api:latest, puspendubanerjee/lexis-web:latest, puspendubanerjee/lexis-uber:latest

./scripts/docker-build.sh --prefix puspendubanerjee/ --platform linux/arm64 --type uber
docker push puspendubanerjee/lexis-uber:arm64-latest
```

The **`uber`** build (`docker/uber.Dockerfile`, `docker-compose.uber.yml`) bundles
the SPA and the API in one image on one port — no nginx. Use the default split
build when you want to scale or deploy the UI and API separately.

Both containers currently run as root and there's no HTTPS/reverse-auth in front of
them - fine for local/trusted-network use, but harden before exposing publicly.

### Using Podman instead

The Dockerfiles pin fully-qualified base images (`docker.io/...`) so rootless
Podman resolves them without prompting, and `docker-compose.yml` is a plain
Compose file both engines read. Use **`podman compose`** (Podman 4.7+), which
shells out to the Compose CLI (`docker compose` / `docker-compose`) pointed at the
Podman socket - so healthchecks and `depends_on: condition: service_healthy`
behave exactly as with Docker. The older standalone `podman-compose` package
honours neither and is not recommended here. The API healthcheck runs a script
file (`docker/healthcheck.py`) rather than an inline `python -c "..."` because
Podman mangles multi-word exec-form healthcheck commands.

```bash
# one-time: start the rootless API socket the Compose provider talks to
systemctl --user enable --now podman.socket

export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/podman/podman.sock"
podman compose up -d --build          # same flags as `docker compose`
```

Or build the images directly with Podman (no socket needed):

```bash
CONTAINER_ENGINE=podman ./scripts/docker-build.sh   # also auto-detected if docker isn't on PATH
```

Notes for rootless Podman: the `lexis-data` named volume lives under
`~/.local/share/containers/storage/volumes/` (not a Docker volume); published
ports `8080`/`8000` are >1024 so no privileged-port config is needed; and the
in-container root user maps to your host UID, so the SQLite file on the volume is
owned by you.

## Connecting to Snowflake or an external DuckDB file

Beyond the demo dataset and one-off `.duckdb`/`.db` uploads, you can register a
named, reusable **connection** and point any model's "Run" at it instead. Two
types are supported: `duckdb_file` (a DuckDB database file already sitting on the
API server's filesystem) and `snowflake`.

Any authenticated user can view/test/run against any connection (same
workspace-wide visibility as models); creating, updating, or deleting one
requires the Editor or Admin role, and only the connection's owner (or an Admin)
can update/delete it. Requests are attributed via the `X-Account-Id` header, same as
everywhere else in the API (see Quickstart: Web UI above).

**Create a DuckDB-file connection:**

```bash
curl -X POST http://localhost:8000/api/connections \
  -H "X-Account-Id: 2" -H "Content-Type: application/json" \
  -d '{
        "name": "local-warehouse",
        "type": "duckdb_file",
        "config": {"path": "/data/warehouse.duckdb"}
      }'
```

`path` is resolved on the **API server** (or, in Docker, inside the
`lexis-api` container) - it's not a client-side file picker. If you're
running via `docker compose`, mount the directory containing the file into the
container (alongside the existing `lexis-data` volume in
`docker-compose.yml`) so the path is reachable there.

**Create a Snowflake connection:**

```bash
curl -X POST http://localhost:8000/api/connections \
  -H "X-Account-Id: 2" -H "Content-Type: application/json" \
  -d '{
        "name": "prod-snowflake",
        "type": "snowflake",
        "config": {
          "account": "xy12345.us-east-1",
          "user": "LEXIS_SVC",
          "password_env": "SNOWFLAKE_PASSWORD",
          "warehouse": "COMPUTE_WH",
          "database": "ANALYTICS",
          "schema": "PUBLIC",
          "role": "ANALYST"
        }
      }'
```

`account`/`user`/`password_env` are required; `warehouse`/`database`/`schema`/
`role` are optional. Secrets are never stored in the database: `password_env` is
the *name* of an environment variable, and the API process reads the actual
password from its own environment (`export SNOWFLAKE_PASSWORD=...`, or an
`environment:` entry in `docker-compose.yml`) at connect time - so that variable
must be set wherever the API process runs, not passed in the request body.

**Test connectivity** (opens a real connection, no query run):

```bash
curl -X POST http://localhost:8000/api/connections/1/test -H "X-Account-Id: 2"
# {"ok": true, "detail": "connected successfully"}
```

**Run a model's metric against a connection** (`connection_id` is the id from
the create response above; same `/run` endpoint used for demo/upload, with
`mode=connection`):

```bash
curl -X POST http://localhost:8000/api/models/1/run \
  -H "X-Account-Id: 2" \
  -F "mode=connection" -F "connection_id=1" \
  -F "metric=total_sales" -F 'group_by_json=["item.i_category"]'
```

The time-series endpoint (`/api/models/{id}/run/timeseries`) takes the same
`mode=connection`/`connection_id` fields alongside its usual `time_dataset`/
`time_field`/`grain`/`filter_grain`/`filter_value` form fields. For a
`duckdb_file` connection, every dataset referenced by the metric/group-by must
share one catalog name (the first `.`-segment of the dataset's `source` in the
Ossie model) - the file is attached under that name, mirroring how the demo/upload
modes work. Snowflake has no such restriction: `source` is used as-is, so it can
reference any `database.schema.table` the connection's role can see.

The web UI's **Connections** page (linked from the header) covers all of the above
graphically - create/edit/delete/test a connection, with the same RBAC - and the
model "Run" tab's "Saved connection" mode lets you pick one to run against.

## Using the live MCP server

The `--target mcp` transpile output above is schema-only — it describes the tools but
doesn't run anything. For an AI tool to actually call a metric and get real query
results back, Lexis can also serve a model as a **live** MCP server, one
`query_<metric>` tool per metric, resolved against the demo dataset, a local DuckDB
file, or Snowflake.

Each `query_<metric>` tool takes `group_by` (dimension refs, constrained to an enum)
**or** `time_grain` (`day`/`week`/`month`/`quarter`/`year`) to get a period-by-period
trend instead of a single total — e.g. "sales by week". `time_grain` buckets are ISO
8601 periods (weeks start Monday); a model whose calendar differs (e.g. a US retail
Sunday–Saturday week) says so in its `ai_context`, surfaced to the client as the MCP
server's `instructions` and in `list_metrics`. `time_field` picks the date axis when
a model has more than one.

There are three ways to wire a client up to it, depending on what you're doing:

| # | Approach | Reaches localhost? | Needs public exposure? |
|---|---|---|---|
| 1 | **Local (stdio)** — the client spawns `lexis mcp-serve` itself | n/a (same process tree) | No |
| 2 | **`mcp-remote` bridge** — the client spawns `mcp-remote`, which proxies to `lexis_api` over plain local HTTP | Yes, from the same machine | **No** |
| 3 | **Public tunnel** (cloudflared/ngrok) — for Claude's own *remote connector* UI, which calls from Anthropic's cloud, not your laptop | No — genuinely public | **Yes** |

Approaches 2 and 3 both talk to the same [remote HTTP endpoint](#the-remote-http-endpoint-approaches-2-and-3);
approach 2 is the better choice whenever you just want to exercise that endpoint
yourself, since it never leaves your machine.

### Approach 1: local (stdio) — e.g. Claude Desktop or any MCP client that launches a subprocess

```bash
pip install -e ".[mcp]"
lexis mcp-serve tests/fixtures/tpcds_semantic_model.yaml --demo
# larger bundled dataset (10,000 facts, 17 metrics):
#   lexis mcp-serve src/lexis_api/sample_data/retail_analytics_model.yaml --demo
# or: --duckdb-file /path/to/warehouse.duckdb
# or: --snowflake-account ... --snowflake-user ... --snowflake-password-env ...
```

`--demo` serves the bundled dataset whose catalog matches the model
(`tpcds.*` → the TPC-DS fixture, `retail.*` → the retail analytics dataset); see
[The bundled demo datasets](#the-bundled-demo-datasets).

Point a client's config at it, e.g. Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lexis": {
      "command": "lexis",
      "args": ["mcp-serve", "/path/to/model.yaml", "--demo"]
    }
  }
}
```

`--duckdb-file`/`--snowflake-*` requires every dataset the model's metrics touch to
be reachable the same way the corresponding **Connection** run mode already requires
(see "Connecting to Snowflake or an external DuckDB file" above) — a `--duckdb-file`
model's datasets must all share one catalog name. If a metric references a table your
dataset doesn't have (e.g. running `--demo` against a model with a `store` dataset,
which isn't in the bundled demo data), that one tool call fails with the underlying
DB error — other metrics keep working.

### The remote HTTP endpoint (approaches 2 and 3)

Mounted on the API, bound to an existing saved Connection:

```text
POST/GET/DELETE /api/models/{model_id}/mcp?connection_id=<id>
```

Requires the same `X-Account-Id` header as the rest of the API, and a `connection_id`
for a Connection you've already created (see above) — the endpoint has no demo/upload
mode, since a remote MCP client can't provide a file per request. `connection_id` is
**not** the model's id, and there's no connection until you create one — a fresh
install has none, so calling this endpoint before creating a connection fails with
`{"detail":"connection not found"}`. If you just want to try it against the bundled
demo data:

```bash
lexis export-demo-dataset --out /tmp/tpcds-demo.duckdb --force

curl -X POST http://localhost:8000/api/connections \
  -H "X-Account-Id: 2" -H "Content-Type: application/json" \
  -d '{"name":"demo","type":"duckdb_file","config":{"path":"/tmp/tpcds-demo.duckdb"}}'
# -> note the "id" in the response, use it as connection_id below
```

Then point any MCP client that supports a remote HTTP server at the model's `/mcp`
URL; it speaks the standard MCP Streamable HTTP transport, e.g.:

```bash
curl -X POST "http://localhost:8000/api/models/1/mcp?connection_id=<id-from-above>" \
  -H "X-Account-Id: 2" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                  "clientInfo":{"name":"curl","version":"0"}}}'
```

Each tool call opens the connection fresh (same per-request cost model the `/run`
endpoint already has) and returns the metric's real result rows, not just the schema.

#### A workspace-wide alternative: switch models/connections without reconnecting

The endpoint above fixes one model+connection in the URL - reasonable for a client
that only ever cares about one model, but it means picking a different model or
connection means reconnecting to a different URL. `POST/GET/DELETE /api/mcp` (no
`model_id`/`connection_id` in the URL at all) is the alternative: one connection,
four generic tools, `model_id`/`connection_id` supplied as **tool-call arguments**
instead:

- `list_models` - every model in the workspace (id, name, description)
- `list_connections` - every connection (id, name, type)
- `list_metrics(model_id)` - a model's metrics, each with its description and valid
  `group_by` references, plus the model's `time_fields` / `time_grains` and any
  model-level `instructions` (the same data the per-model endpoint bakes into each
  `query_<metric>` tool's schema, just returned as data here instead)
- `query_metric(model_id, metric, connection_id, group_by? | time_grain? + time_field?)` - runs it

The trade-off: the per-model endpoint's one-governed-tool-per-metric design (a
distinct `query_<metric>` tool, `group_by` constrained to a real enum in the JSON
schema itself) becomes one generic `query_metric` tool instead, since the tool
schema can no longer depend on which `model_id` shows up in a given call - an
agent has to call `list_metrics` first to discover what's valid rather than having
it enforced by the schema. Point either the `mcp-remote` bridge (below) or a
tunnel at `http://localhost:8000/api/mcp` instead of the per-model URL to use it.

### Approach 2: `mcp-remote` as a local stdio bridge (no public exposure)

When you add a **custom (remote) connector** in Claude Desktop's Settings → Connectors
or claude.ai, Anthropic's cloud infrastructure — not your local Desktop app — is what
actually opens the HTTP connection to the URL you give it. `localhost` from their
servers' point of view means *their own server*, not your laptop, so that path
genuinely requires a publicly reachable URL (approach 3, below).

If you just want to exercise the remote HTTP endpoint above without any public
exposure, [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) is a small local
bridge: Claude spawns it as an ordinary local (stdio) subprocess — same mechanism as
approach 1 — and *it* makes the HTTP call to `lexis_api` from your own machine, where
`localhost` means exactly what you'd expect. No tunnel, no TLS, no public exposure:

```json
{
  "mcpServers": {
    "lexis-remote": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://localhost:8000/api/models/1/mcp?connection_id=1",
        "--allow-http",
        "--header",
        "X-Account-Id: 2"
      ]
    }
  }
}
```

`--allow-http` is required since the endpoint here is plain HTTP (`mcp-remote` refuses
non-HTTPS URLs by default, for good reason on a real network — this one never leaves
your machine). `--header` supplies the same `X-Account-Id` auth the API needs everywhere
else. Verified working end-to-end: `initialize` → `notifications/initialized` →
`tools/call query_total_sales` all round-trip correctly through the bridge.

### Approach 3: exposing the endpoint over HTTPS (for Claude's own remote connector)

If you specifically want to use Claude's built-in remote-connector UI (rather than
`mcp-remote`), you do need a genuinely public HTTPS URL, for the reason above. For
local development, the quickest way to get one is a tunnel that terminates TLS for you
and forwards to your local port, with no cert management needed:

```bash
# install once (see cloudflare's docs for your OS if `brew`/`apt` aren't available)
brew install cloudflared   # or: apt install cloudflared

# with lexis_api already running on :8000 - logging to .dev/ alongside
# scripts/dev.sh's own api.log/web.log, backgrounded so the shell stays free:
nohup cloudflared tunnel --url http://localhost:8000 > .dev/cloudflared.log 2>&1 &
sleep 5 && grep -o 'https://[a-zA-Z0-9.-]*trycloudflare\.com' .dev/cloudflared.log
```

That prints the random `https://<something>.trycloudflare.com` URL straight from the
log. Your full connector URL is then:

```text
https://<something>.trycloudflare.com/api/models/<model_id>/mcp?connection_id=<connection_id>
```

**ngrok** is an equivalent alternative if you'd rather use that:

```bash
ngrok http 8000
```

This gives a random `https://<random>.ngrok-free.app` URL, same trade-off as
`cloudflared`'s quick tunnel above — it changes every restart, so the connector URL
needs re-entering each time (and since Claude's custom connectors have no edit
option, that means delete-and-recreate, not just editing a field).

ngrok's free tier includes **one static/reserved domain per account**, which avoids
that entirely - the URL stays the same across restarts:

1. Claim it once: ngrok dashboard → Universal Edge → Domains → **+ New Domain**
   (gives you something like `engaging-expose-annex.ngrok-free.dev`).
2. Bind it directly with `--domain` - no config file needed for a single stable tunnel:

   ```bash
   ngrok http --domain=engaging-expose-annex.ngrok-free.dev 8000
   ```

   Or, for a named, reusable config (`ngrok start <name>`), add it under `endpoints`/
   `tunnels` in `~/.config/ngrok/ngrok.yml` with that domain + port, then
   `ngrok start <name>`.
3. Your connector URL is now fixed:
   `https://engaging-expose-annex.ngrok-free.dev/api/models/<model_id>/mcp?connection_id=<connection_id>`
   (or `/api/mcp` for the [workspace-wide endpoint](#a-workspace-wide-alternative-switch-modelsconnections-without-reconnecting) —
   never needs updating either way, since the domain doesn't change).

One gotcha specific to a *named* tunnel: ngrok only allows one running agent per
static domain at a time, across every machine on your account - if you get
`ERR_NGROK_334` ("endpoint is already online"), an earlier session (a different
terminal, a different device) still has it claimed; stop that one first, or check
the ngrok dashboard's Agents/Endpoints page to disconnect it remotely. [ngrok's
static domains blog post](https://ngrok.com/blog/free-static-domains-ngrok-users)

#### Tunneling the UI too, on the same port

ngrok's (and most tunnel providers') free tier gives you exactly one exposed
port/endpoint. If you also want to share the running web UI - not just the MCP
endpoint - through that same single tunnel, `lexis_api` can proxy its own port to
the Vite dev server, so one `uvicorn` port serves both:

```bash
# via scripts/dev.sh (derives the target from LEXIS_WEB_PORT automatically):
LEXIS_DEV_UI_PROXY=1 ./scripts/dev.sh start

# or standalone:
LEXIS_DEV_UI_PROXY_TARGET=http://localhost:5173 uvicorn lexis_api.main:app --port 8000
```

With that set, `:8000` serves `/api/...` as usual and forwards everything
else (including Vite's HMR WebSocket) to the dev server - so `ngrok http 8000` (or
the cloudflared tunnel above) now exposes the whole app, not just the API. It's
off unless that env var is set, and it's dev-only by design - production
(`docker-compose`) already has this same job done by `nginx` instead
(`docker/nginx.conf`), which this proxy doesn't replace or touch. This also
sidesteps Vite's own `Host`-header check (the "Blocked request... add to
`server.allowedHosts`" error) automatically, since the proxy always presents
itself to Vite as `localhost:5173` regardless of the tunnel's public hostname.

**Before you expose it**: `X-Account-Id` is a development-only auth stub in this codebase
(see `lexis_api/deps.py`) — anyone who reaches the tunnel URL can act as *any* user id
just by setting that header themselves, no password or token required, **unless** the
tunnel has Google-backed edge auth in front of it. Both ngrok and Cloudflare (the two
tunnels this README covers) support that, with one practical difference:

- **ngrok** — the [`oauth` traffic-policy action](https://ngrok.com/docs/traffic-policy/actions/oauth/)
  can authenticate against Google with *zero* Google-side setup: omit
  `client_id`/`client_secret` from the `google` provider config and ngrok authenticates
  visitors through its own managed Google OAuth app instead of one you'd have to
  register yourself.

  ```yaml
  # traffic-policy.yaml
  on_http_request:
    - actions:
        - type: oauth
          config:
            provider: google
  ```

  ```bash
  ngrok http 8000 --traffic-policy-file traffic-policy.yaml
  ```

  It injects `X-User-Email` (+ `X-User-Name`) on requests that pass.

- **Cloudflare Access** (in front of the `cloudflared` tunnel above) — needs a (free)
  Zero Trust account, and unlike ngrok there's no shared Google app: register your own
  OAuth Client ID/Secret in Google Cloud Console first.
  1. Zero Trust dashboard → **Settings → Authentication → Login methods → Add → Google**,
     paste that Client ID/Secret (redirect URI Google needs:
     `https://<your-team-name>.cloudflareaccess.com/cdn-cgi/access/callback`).
  2. **Access → Applications → Add an application → Self-hosted**, set the application
     domain to your tunnel's public hostname, add a policy that allows the
     emails/domain you want signed in with Google as the identity provider, save.
  3. Run `cloudflared` as usual (see above) — Access enforces the policy at
     Cloudflare's edge, before any request reaches `cloudflared` or this app, and
     injects `Cf-Access-Authenticated-User-Email` on requests that pass.

When either header is present the backend authenticates with that real Google identity
instead and ignores `X-Account-Id` entirely (see the Quickstart section above and
`lexis_api/deps.get_current_user`) — so a tunnel fronted by either of these is safe to
leave up. Without one, treat the exposure exactly as before: only run the tunnel while
you're actively testing against your own machine, don't point it at a database with real
data, and kill it (`pkill cloudflared`, since the command above backgrounds it) as soon
as you're done — the hostname is random and will change on every restart anyway, so
there's no persistent URL to protect.

**Registering the Google OAuth client for Cloudflare Access**: when you create the OAuth
Client ID in Google Cloud Console (APIs & Services → Credentials → OAuth consent
screen), it'll flag your app's homepage as not explaining the app's purpose, having an
insufficient privacy policy, and sitting behind a login page — all because Access gates
the *entire* origin, so Google's checker can't see anything. The app's `/` route is a
homepage for exactly this (`frontend/src/pages/HomePage.tsx`, alongside `/privacy` and
`/terms` - `PrivacyPolicyPage.tsx` / `TermsOfServicePage.tsx` - all linked from the
footer and none dependent on being signed in) — point the consent screen's "Application
home page" at `<your-domain>` and its "Privacy policy" field at `<your-domain>/privacy`.
The actual model-authoring app lives at `/models` instead (the "Models" nav link) - only
`/`, `/privacy`, and `/terms` are meant to be public.

To let Google's checker (and anyone else) actually reach those without signing in, add a
second, narrower Access application scoped to just those paths with a **Bypass** policy
(Access → Applications → Add an application → Self-hosted, hostname `<your-domain>`,
paths `/`, `/privacy`, `/terms`, one policy with action Bypass). Two things worth
double-checking once that's in place:

- The frontend is a client-rendered SPA (a `<div id="root">` filled in by JS - see
  `frontend/dist/index.html`), so bypassing `/` alone isn't enough - the bundled JS/CSS
  under `/assets/*` needs bypassing too, or the "public" pages will just show a blank
  page (or a login redirect) to anyone not already signed in. Add that path to the same
  Bypass application.
- Cloudflare resolves overlapping paths by specificity (the most specific match wins,
  with no inheritance from a shorter one), so a Bypass on `/` shouldn't widen to cover
  `/models` or `/connections` if those are matched by a separate, more specific
  Access application requiring login - but this is exactly the kind of policy
  interaction worth verifying yourself (an incognito window against the real domain)
  rather than trusting written-down path lists blindly.

While you're in the consent screen, also make sure its **App name** field matches what
the app actually calls itself ("Lexis"), since a mismatch there is its own verification
warning. For most personal/small-team deployments it's simpler to skip verification
entirely instead: leave the consent screen's **Publishing status** at **Testing** and add
your own Google
account(s) under **Audience → Test users** — sign-in works immediately, with none of the
above required, since verification review only applies when publishing to "In
production."

### Connecting Claude's remote connector to it (approach 3 only)

1. Claude Desktop: `Ctrl+,` (or the top-left menu → File → Settings) → **Connectors**
   in the sidebar → **Add custom connector**.
2. Paste in the tunnel URL from above, including the `/api/models/<model_id>/mcp?connection_id=<connection_id>` path.
3. Look for a **Request headers** section in that same dialog and add `X-Account-Id` →
   your user id (e.g. `2`). Claude stores it as the connector's credential and sends it
   on every request — this is what satisfies the endpoint's auth requirement.

Request-header support for custom connectors is currently a **beta feature limited to
some organizations** — if you don't see that section, the dialog will only offer OAuth,
which this endpoint doesn't implement, and you won't be able to connect this
particular remote endpoint from Claude's UI without a small proxy in front of it that
injects the header for you. Approaches 1 and 2 above have no such limitation — neither
goes through this connector-UI auth path at all, since both configure the header (or
skip auth entirely) directly in `claude_desktop_config.json`.

### Sample questions to ask

Once connected (any of the three approaches), the model's `ai_context` synonyms let
you ask in plain language instead of naming the metric exactly.

Against the **`tpcds`** model (`lexis mcp-serve tests/fixtures/tpcds_semantic_model.yaml --demo`):

- "How's revenue breaking down by product category?"
- "What's our customer lifetime value?"
- "Break total sales down by brand."
- "Show me sales by year."
- "How productive are our stores?" (exercises `store_productivity`, a ratio metric)

Against the **`retail`** model (`lexis mcp-serve src/lexis_api/sample_data/retail_analytics_model.yaml --demo`),
which has enough data for the answers to be interesting:

- "What's total revenue and gross margin percent by product category?"
- "Show revenue by month — is there a Q4 bump?"
- "Which store format has the highest revenue per employee?"
- "Break average basket value down by customer loyalty tier."
- "How much are we losing to returns, and what's the top return reason?"

Two things worth knowing about the small **`tpcds`** `--demo` data specifically, so
unexpected answers don't read as bugs: it's only 2 items/2 customers, so per-item
attributes like brand and category are perfectly correlated (slicing by either gives
the same split); and a handful of `store_sales` rows deliberately reference an
item/customer id that doesn't exist, so an item- or customer-sliced metric will show a
smaller total than one sliced by date alone — that's correct `INNER JOIN` behavior on
intentionally incomplete sample data. The **`retail`** dataset has none of these
quirks: every fact row's foreign keys resolve, and the dimensions are fully populated.

## Running tests

```bash
pip install -e ".[dev]"       # core library + CLI tests only
pytest tests --ignore=tests/api

pip install -e ".[dev,api]"   # everything, including the API test suite
pytest
```

## Project structure

```text
src/lexis/          core library: Ossie parsing, join-graph resolution, transpilers, CLI
src/lexis/demo_data.py         small fixed TPC-DS demo dataset (in-memory / exported .duckdb)
src/lexis/retail_demo_data.py  generated 10,000-fact retail analytics demo dataset
src/lexis_api/      FastAPI backend (models, RBAC, transpile route, live query execution
                        against demo/upload DuckDB or a persisted connections.py connection)
src/lexis_api/sample_data/     bundled models seeded on first boot (tpcds + retail_analytics)
frontend/                Vite + React + TypeScript SPA
tests/                  core library tests (fixtures under tests/fixtures/)
tests/api/              backend API tests
docs/architecture-plan.md   architecture decisions and design rationale
docker/                 Dockerfiles + nginx config (split: backend/frontend; single: uber)
scripts/docker-build.sh   builds images directly with `docker build` (--type split|uber|all,
                        --platform, --prefix), no compose
docker-compose.uber.yml   single-container variant (SPA + API in one image; + .uber.demo.yml overlay)
third_party/ossie/      git submodule: upstream Ossie spec/schema/converters docs/examples
```

## Keeping Ossie in sync

`src/lexis/_vendor/ossie/models.py` is a vendored (not pip-installed - `apache-ossie`
isn't on PyPI yet) copy of upstream's pydantic model classes, and `tests/fixtures/*.yaml`
are meant to conform to upstream's JSON Schema. Both are checked against the
`third_party/ossie` submodule by `tests/test_ossie_spec_conformance.py`, so a submodule bump
that changes either will fail loudly instead of silently drifting.

To pick up an upstream Ossie change:

```bash
git submodule update --remote third_party/ossie   # bump the submodule to upstream's latest main
scripts/sync_ossie_vendor.sh                        # re-vendor models.py + refresh NOTICE.md's commit pin
pytest tests/test_ossie_spec_conformance.py tests/test_parser.py tests/test_resolved_model.py
```

`scripts/sync_ossie_vendor.sh` only overwrites `models.py` verbatim; `__init__.py` is
hand-adapted (relative import, own docstring) and the script just warns if a new
upstream class/name isn't re-exported yet, so it needs a manual one-line addition in
that case.

`third_party/ossie` is upstream's repo, not ours - never edit files inside it directly,
and never commit local changes to it (we have no push access, and a submodule pointer
referencing a commit we made locally but never pushed would break for everyone else
who clones this repo). Its `.gitmodules` entry sets `ignore = dirty`, so `git status`/
`git diff` won't even show local edits inside it; the only supported way to move it
forward is `git submodule update --remote third_party/ossie` followed by
`scripts/sync_ossie_vendor.sh`.

This is also enforced by two automated checks, both running
`scripts/check_ossie_submodule_pin.sh` (fails if `third_party/ossie` is pinned to a commit
that isn't reachable from any of its remote branches - i.e. a local-only commit made by
accidentally `cd`-ing into the submodule and committing there):

- **CI** (`.github/workflows/check-ossie-submodule.yml`) runs it on every push/PR - the
  real backstop, since it can't be skipped.
- **A local pre-commit hook** (`.githooks/pre-commit`) runs it before any commit that
  touches the submodule pin, so you find out before pushing rather than after CI fails.
  Opt in once per clone (git doesn't version `.git/hooks`, so this isn't automatic):

  ```bash
  git config core.hooksPath .githooks
  ```

## License

Apache 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
