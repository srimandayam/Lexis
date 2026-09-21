// Hand-written TS interfaces mirroring lexis_api/schemas.py. No codegen for v1 -
// keep these in sync manually when the backend schemas change.

export type Role = "admin" | "editor" | "viewer";

export interface UserOut {
  id: number;
  username: string;
  role: Role;
  // Set only for a Google-authenticated user (see lexis_api/deps.py); null for the
  // seeded dev-stub users. Its presence on `/api/users/me` is what UserContext uses
  // to tell a real signed-in identity apart from the local dev "Acting as" switcher.
  email: string | null;
}

export interface FieldOut {
  name: string;
  description: string | null;
  expression: string | null;
  is_time: boolean;
  datatype: string | null;
}

export interface DatasetOut {
  name: string;
  source: string;
  fields: FieldOut[];
}

export interface RelationshipOut {
  name: string;
  from_dataset: string;
  to: string;
  from_columns: string[];
  to_columns: string[];
}

export interface MetricOut {
  name: string;
  description: string | null;
  expression: string | null;
  referenced_datasets: string[];
  datatype: string | null;
}

export interface ModelSummaryOut {
  id: number;
  name: string;
  owner_id: number;
  owner_username: string;
  dataset_count: number;
  metric_count: number;
  created_at: string;
  updated_at: string;
}

export interface ModelDetailOut extends ModelSummaryOut {
  raw_yaml: string;
  datasets: DatasetOut[];
  relationships: RelationshipOut[];
  metrics: MetricOut[];
}

export interface CreateModelIn {
  name?: string | null;
  yaml_text: string;
}

export interface UpdateModelIn {
  yaml_text: string;
}

export interface ImportSmlOut {
  model: ModelDetailOut;
  warnings: string[];
}

export type Target =
  | "duckdb"
  | "postgres"
  | "bigquery"
  | "databricks"
  | "snowflake"
  | "cube"
  | "dbt"
  | "mcp"
  | "snowflake_semantic_view"
  | "sml"
  | "lookml";

export const SQL_TARGETS: Target[] = ["duckdb", "postgres", "bigquery", "databricks", "snowflake"];
export const ALL_TARGETS: Target[] = [
  ...SQL_TARGETS,
  "cube",
  "dbt",
  "mcp",
  "snowflake_semantic_view",
  "sml",
  "lookml",
];

export interface TranspileIn {
  target: Target;
  metric?: string | null;
  group_by?: string[] | null;
  // Per-target settings Ossie itself doesn't model. Only `lookml` takes any:
  // `connection` (the Looker connection name) and `dialect`. Sending an option
  // a target doesn't accept is a 422, not a silent no-op.
  options?: Record<string, string> | null;
}

export interface TranspileOut {
  // `sml` and `lookml` are the multi-file targets - one YAML file per SML
  // object, and a views/ + model file project respectively - so `content` is a
  // `Record<string, string>` (relative filename -> content) there; every other
  // target still returns a single `string`.
  content: string | Record<string, string>;
  warnings: string[];
}

export interface RunDuckDbOut {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  sql: string;
}

export interface GraphFieldIn {
  name: string;
  expression: string;
  description?: string | null;
}

export interface GraphDatasetIn {
  name: string;
  source: string;
  fields: GraphFieldIn[];
}

export interface GraphRelationshipIn {
  name: string;
  from_dataset: string;
  to: string;
  from_columns: string[];
  to_columns: string[];
}

export interface GraphMetricIn {
  name: string;
  expression: string;
  description?: string | null;
}

export interface GraphEditIn {
  datasets: GraphDatasetIn[];
  relationships: GraphRelationshipIn[];
  metrics: GraphMetricIn[];
}

export type ConnectionType = "duckdb_file" | "snowflake";

export interface ConnectionOut {
  id: number;
  name: string;
  type: ConnectionType;
  owner_id: number;
  owner_username: string;
  config: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export interface ConnectionIn {
  name: string;
  type: ConnectionType;
  config: Record<string, string>;
}

export interface ConnectionTestOut {
  ok: boolean;
  detail: string;
}
