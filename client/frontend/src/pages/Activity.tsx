import { useMemo, useState } from "react";
import { useApp } from "../context";
import { Button, Empty, Icon, Pill, SectionHeader, CopyButton, formatDate, timeAgo } from "../components/ui";

type Source = "local" | "server" | "signin";

interface Row {
  key: string;
  at: string;
  kind: string;
  level: string;
  message: string;
  fields: Record<string, unknown>;
  source: Source;
}

export default function Activity() {
  const { snapshot, refresh, toast } = useApp();
  const [source, setSource] = useState<"all" | Source>("all");
  const [level, setLevel] = useState("all");
  const [selected, setSelected] = useState<Row | null>(null);

  const rows = useMemo<Row[]>(() => {
    const local: Row[] = snapshot.events.map((event) => ({
      key: `local-${event.id}`,
      at: event.at,
      kind: event.kind,
      level: event.level,
      message: event.message,
      fields: event.fields || {},
      source: "local"
    }));
    const server: Row[] = (snapshot.operator.events || []).map((event, index) => {
      const record = event as Record<string, unknown>;
      const payload = (record.payload || record.data || {}) as Record<string, unknown>;
      return {
        key: `server-${record.id ?? index}`,
        at: String(record.created_at || record.at || ""),
        kind: String(record.kind || record.type || "server.event"),
        level: String(record.level || "info"),
        message: String(record.message || payload.message || record.type || record.kind || "Server event"),
        fields: payload,
        source: "server"
      };
    });
    const signin: Row[] = snapshot.auth_history.map((row) => ({
      key: `auth-${row.id}`,
      at: row.started_at,
      kind: `agent.auth.${row.outcome || row.status}`,
      level: row.outcome === "authenticated" ? "info" : "warning",
      message: `${row.kind} sign-in ${row.outcome}; CLI status ${row.auth_status}.`,
      fields: { kind: row.kind, mode: row.mode, exit_code: row.exit_code, detail: row.auth_detail, challenge: row.challenge },
      source: "signin"
    }));
    return [...local, ...server, ...signin].sort((a, b) => Date.parse(b.at || "") - Date.parse(a.at || ""));
  }, [snapshot.events, snapshot.operator.events, snapshot.auth_history]);

  const filtered = rows.filter((row) => {
    if (source !== "all" && row.source !== source) return false;
    if (level !== "all" && row.level !== level) return false;
    return true;
  });

  const counts = rows.reduce<Record<string, number>>((acc, row) => {
    acc[row.source] = (acc[row.source] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="page">
      <SectionHeader
        title="Audit timeline"
        subtitle="Local bridge events, sign-in outcomes and (with operator mode) server history. Newest first."
        action={
          <Button size="sm" variant="secondary" icon="refresh" onClick={async () => { await refresh(); toast("Timeline refreshed.", "ok"); }}>
            Refresh
          </Button>
        }
      />

      <div className="toolbar">
        <div className="segmented">
          {(["all", "local", "server", "signin"] as const).map((item) => (
            <button key={item} className={source === item ? "active" : ""} onClick={() => setSource(item)}>
              {item === "all" ? "All sources" : item === "local" ? "Local bridge" : item === "server" ? "Server" : "Sign-in"}
              <b>{item === "all" ? rows.length : counts[item] || 0}</b>
            </button>
          ))}
        </div>
        <select value={level} onChange={(event) => setLevel(event.target.value)}>
          <option value="all">Every level</option>
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="error">Error</option>
        </select>
        <CopyButton value={JSON.stringify(filtered, null, 2)} label="Copy filtered JSON" />
      </div>

      <div className="activity-layout">
        <div className="timeline card">
          {filtered.length === 0 ? (
            <Empty title="No events match" detail="The daemon records real lifecycle events; an empty filter is a real result." />
          ) : (
            filtered.map((row) => (
              <button key={row.key} className={`timeline-row activity-row ${selected?.key === row.key ? "active" : ""}`} onClick={() => setSelected(row)}>
                <span className={`timeline-dot ${row.level}`} />
                <div className="activity-main">
                  <div className="row">
                    <strong>{row.message}</strong>
                    <Pill tone={row.source === "server" ? "violet" : row.source === "signin" ? "blue" : row.level === "error" ? "red" : row.level === "warning" ? "amber" : "green"}>
                      {row.source}
                    </Pill>
                  </div>
                  <span>
                    <span className="mono">{row.kind}</span> · {timeAgo(row.at)}
                  </span>
                </div>
                <Icon name="arrow" size={15} />
              </button>
            ))
          )}
        </div>

        <aside className="card activity-detail">
          {selected ? (
            <>
              <div className="card-head">
                <div>
                  <h2>{selected.kind}</h2>
                  <p>{formatDate(selected.at)}</p>
                </div>
                <Pill tone={selected.level === "error" ? "red" : selected.level === "warning" ? "amber" : "green"}>{selected.level}</Pill>
              </div>
              <p className="activity-message">{selected.message}</p>
              <dl className="kv tight">
                <div><dt>Source</dt><dd>{selected.source}</dd></div>
                <div><dt>Recorded</dt><dd>{formatDate(selected.at)}</dd></div>
              </dl>
              <h4 className="small-head">Fields</h4>
              <pre className="code-block">{JSON.stringify(selected.fields, null, 2)}</pre>
            </>
          ) : (
            <div className="activity-placeholder">
              <Icon name="activity" size={26} />
              <h3>Choose an event</h3>
              <p>Select a row to inspect its structured fields. Nothing is synthesized: the JSON is what the daemon or server returned.</p>
              {!snapshot.operator.available && (
                <p className="small muted">Server history is hidden until operator mode is enabled.</p>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
