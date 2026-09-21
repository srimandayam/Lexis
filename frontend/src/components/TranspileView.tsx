import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../api/client";
import { ALL_TARGETS, SQL_TARGETS, type ModelDetailOut, type Target } from "../api/types";
import { FileTree } from "./FileTree";
import { fieldRefs } from "../lib/fieldRefs";

export function TranspileView({ model }: { model: ModelDetailOut }) {
  const [target, setTarget] = useState<Target>("duckdb");
  const [metric, setMetric] = useState(model.metrics[0]?.name ?? "");
  const [groupBy, setGroupBy] = useState<string[]>([]);
  const [lookmlConnection, setLookmlConnection] = useState("");
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const isSql = (SQL_TARGETS as string[]).includes(target);
  const isLookml = target === "lookml";
  const refs = fieldRefs(model);

  const mutation = useMutation({
    mutationFn: () =>
      api.transpile(model.id, {
        target,
        metric: isSql ? metric : undefined,
        group_by: isSql && groupBy.length > 0 ? groupBy : undefined,
        // Omitted when blank so the emitter falls back to its placeholder and
        // warns, rather than being handed an empty connection name (a 422).
        options:
          isLookml && lookmlConnection.trim() !== ""
            ? { connection: lookmlConnection.trim() }
            : undefined,
      }),
    onSuccess: (data) => {
      setActiveFile(typeof data.content === "string" ? null : (Object.keys(data.content)[0] ?? null));
    },
  });

  const files = mutation.data && typeof mutation.data.content !== "string" ? mutation.data.content : null;

  return (
    <div className="stack">
      <div className="row">
        <label>
          Target{" "}
          <select value={target} onChange={(e) => setTarget(e.target.value as Target)}>
            {ALL_TARGETS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>

        {isLookml && (
          <label>
            Looker connection{" "}
            <input
              value={lookmlConnection}
              onChange={(e) => setLookmlConnection(e.target.value)}
              placeholder="lexis_connection"
            />
          </label>
        )}

        {isSql && (
          <label>
            Metric{" "}
            <select value={metric} onChange={(e) => setMetric(e.target.value)}>
              {model.metrics.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}
                </option>
              ))}
            </select>
          </label>
        )}

        <button className="primary" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
          {mutation.isPending ? "Transpiling…" : "Transpile"}
        </button>
      </div>

      {isSql && (
        <div className="field-row">
          <label>Group by (optional)</label>
          <select
            multiple
            size={Math.min(6, refs.length)}
            value={groupBy}
            onChange={(e) => setGroupBy(Array.from(e.target.selectedOptions, (o) => o.value))}
          >
            {refs.map((ref) => (
              <option key={ref} value={ref}>
                {ref}
              </option>
            ))}
          </select>
        </div>
      )}

      {mutation.isError && (
        <p className="error">
          {mutation.error instanceof ApiError ? JSON.stringify(mutation.error.detail) : String(mutation.error)}
        </p>
      )}

      {mutation.data && (
        <div className="stack">
          {mutation.data.warnings.map((w, i) => (
            <p key={i} className="error">
              warning: {w}
            </p>
          ))}
          {files ? (
            <div className="file-explorer">
              <FileTree files={files} activeFile={activeFile} onSelect={setActiveFile} />
              <pre>{activeFile ? files[activeFile] : "Select a file"}</pre>
            </div>
          ) : (
            <pre>{mutation.data.content as string}</pre>
          )}
        </div>
      )}
    </div>
  );
}
