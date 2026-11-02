import { useMemo, useState } from "react";
import { useApp } from "../context";
import type { TaskRecord } from "../types";
import {
  Button,
  Empty,
  Icon,
  Modal,
  Pill,
  Progress,
  formatDate,
  timeAgo,
  taskStatusLabel,
  taskStatusTone
} from "../components/ui";

const FILTERS = ["all", "running", "queued", "completed", "failed"] as const;
type Filter = (typeof FILTERS)[number];

export default function Tasks() {
  const { snapshot, refresh, toast } = useApp();
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<TaskRecord | null>(null);

  const counts = useMemo(() => {
    const result: Record<string, number> = { all: snapshot.tasks.length };
    for (const task of snapshot.tasks) {
      const status = task.status || "unknown";
      result[status] = (result[status] || 0) + 1;
      if (["completed", "succeeded"].includes(status)) result.completed = (result.completed || 0) + 1;
      if (["failed", "cancelled", "interrupted"].includes(status)) result.failed = (result.failed || 0) + 1;
    }
    return result;
  }, [snapshot.tasks]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return snapshot.tasks.filter((task) => {
      const status = task.status || "";
      if (filter === "running" && !["running", "assigned"].includes(status)) return false;
      if (filter === "queued" && !["queued", "awaiting_approval", "blocked"].includes(status)) return false;
      if (filter === "completed" && !["completed", "succeeded"].includes(status)) return false;
      if (filter === "failed" && !["failed", "cancelled", "interrupted"].includes(status)) return false;
      if (!needle) return true;
      return [task.id, task.title, task.description, task.worker_name, task.agent_kind, task.project_id]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle));
    });
  }, [snapshot.tasks, filter, query]);

  const running = snapshot.tasks.filter((task) => task.status === "running").length;

  return (
    <div className="page">
      <div className="toolbar">
        <div className="segmented">
          {FILTERS.map((item) => (
            <button key={item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>
              {item === "all" ? "All" : item === "running" ? "Active" : item === "queued" ? "Waiting" : item === "completed" ? "Completed" : "Failed"}
              <b>{counts[item === "running" ? "running" : item] ?? counts[item] ?? 0}</b>
            </button>
          ))}
        </div>
        <div className="search">
          <Icon name="eye" size={16} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter by id, title, agent or project" />
        </div>
        <Button size="sm" variant="secondary" icon="refresh" onClick={async () => { await refresh(); toast("Task list refreshed.", "ok"); }}>
          Refresh
        </Button>
      </div>

      <div className="task-summary">
        <span><i className={running ? "live" : ""} /> {running ? `${running} running now` : "No running task"}</span>
        <span>{snapshot.worker.tasks_pending} pending claim{snapshot.worker.tasks_pending === 1 ? "" : "s"}</span>
        <span>Worker {snapshot.worker.pid ? `PID ${snapshot.worker.pid}` : "stopped"}</span>
      </div>

      {filtered.length === 0 ? (
        <Empty
          title={snapshot.tasks.length === 0 ? "No tasks yet" : "No task matches this filter"}
          detail={
            snapshot.tasks.length === 0
              ? "Tasks appear here after the server assigns work to this worker. The list is a merge of local execution state and the server's task rows."
              : "Clear the search or choose another status group."
          }
          action={snapshot.tasks.length === 0 ? (
            <Button size="sm" variant="secondary" icon="server" onClick={() => toast("The worker claims tasks from the server push; there is no local create-task endpoint.", "warn")}>
              How tasks arrive
            </Button>
          ) : undefined}
        />
      ) : (
        <div className="task-table card flush">
          <div className="task-table-head">
            <span>Task</span>
            <span>Status</span>
            <span>Agent / worker</span>
            <span>Progress</span>
            <span>Updated</span>
          </div>
          {filtered.map((task) => (
            <button key={task.id} className="task-table-row" onClick={() => setSelected(task)}>
              <div className="task-cell-main">
                <strong>{task.title || task.id}</strong>
                <span className="mono small">{task.id}{task.project_id ? ` · ${task.project_id}` : ""}</span>
                {task.message && <span className="small muted ellipsis">{task.message}</span>}
              </div>
              <div>
                <Pill tone={taskStatusTone(task.status)}>{taskStatusLabel(task.status)}</Pill>
                {task.priority != null && <span className="small muted block">priority {task.priority}</span>}
              </div>
              <div>
                <span className="small">{task.agent_kind || "—"}{task.model_id ? ` · ${task.model_id}` : ""}</span>
                <span className="small muted block ellipsis">{task.worker_name || task.worker_id || "unassigned"}</span>
              </div>
              <div className="task-cell-progress">
                <Progress value={task.progress ?? (task.status === "completed" ? 100 : 0)} tone={taskStatusTone(task.status) === "green" ? "green" : taskStatusTone(task.status) === "red" ? "red" : "blue"} />
                <span className="small muted">{task.progress != null ? `${task.progress}%` : "—"}</span>
              </div>
              <span className="small muted">{timeAgo(task.updated_at || task.created_at)}</span>
            </button>
          ))}
        </div>
      )}

      {selected && (
        <Modal
          wide
          title={selected.title || selected.id}
          subtitle={selected.description || "Task detail from the merged local/server view."}
          onClose={() => setSelected(null)}
          footer={
            <div className="row spread">
              <span className="mono small">{selected.id}</span>
              <Button variant="secondary" onClick={() => setSelected(null)}>Close</Button>
            </div>
          }
        >
          <div className="task-detail-top">
            <Pill tone={taskStatusTone(selected.status)}>{taskStatusLabel(selected.status)}</Pill>
            {selected.local_status && selected.local_status !== selected.status && (
              <Pill tone="neutral">local: {selected.local_status}</Pill>
            )}
            <span className="small muted">{selected.attempts != null ? `${selected.attempts} attempt(s)` : "attempts unknown"}</span>
          </div>
          <Progress value={selected.progress ?? (selected.status === "completed" ? 100 : 0)} tone={taskStatusTone(selected.status) === "green" ? "green" : taskStatusTone(selected.status) === "red" ? "red" : "blue"} />
          <dl className="kv">
            <div><dt>Project</dt><dd className="mono">{selected.project_id || "—"}</dd></div>
            <div><dt>Agent</dt><dd>{selected.agent_kind || "—"}{selected.model_id ? ` / ${selected.model_id}` : ""}</dd></div>
            <div><dt>Worker</dt><dd>{selected.worker_name || "—"} {selected.worker_id ? <span className="mono small">({selected.worker_id})</span> : null}</dd></div>
            <div><dt>Priority</dt><dd>{selected.priority ?? "—"}</dd></div>
            <div><dt>Created</dt><dd>{formatDate(selected.created_at)}</dd></div>
            <div><dt>Started</dt><dd>{formatDate(selected.started_at)}</dd></div>
            <div><dt>Finished</dt><dd>{formatDate(selected.finished_at)}</dd></div>
            <div><dt>Updated</dt><dd>{formatDate(selected.updated_at)}</dd></div>
          </dl>
          {selected.acceptance && selected.acceptance.length > 0 && (
            <>
              <h4 className="small-head">Acceptance criteria</h4>
              <ul className="bullet-list">
                {selected.acceptance.map((item, index) => <li key={index}>{item}</li>)}
              </ul>
            </>
          )}
          {selected.dependencies && selected.dependencies.length > 0 && (
            <>
              <h4 className="small-head">Dependencies</h4>
              <div className="chip-row">{selected.dependencies.map((item) => <span key={item} className="chip mono">{item}</span>)}</div>
            </>
          )}
          {selected.error && (
            <>
              <h4 className="small-head">Error</h4>
              <pre className="code-block bad-block">{selected.error}</pre>
            </>
          )}
          <h4 className="small-head">Output</h4>
          <pre className="code-block tall">{selected.output || "No output captured for this task yet."}</pre>
          <p className="small muted">
            The local task view is updated by the worker's NDJSON events. The server row is the
            authority for status; this panel does not invent progress.
          </p>
        </Modal>
      )}
    </div>
  );
}
