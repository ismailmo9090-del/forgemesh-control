import { useState } from "react";
import { api, shell } from "../api";
import { useApp } from "../context";
import {
  Button,
  Empty,
  Icon,
  Pill,
  Progress,
  Sparkline,
  StatusBanner,
  availabilityTone,
  connectionLabel,
  formatBytes,
  formatDate,
  timeAgo,
  taskStatusLabel,
  taskStatusTone,
  uptime
} from "../components/ui";

export default function Dashboard({ cpuHistory, memHistory }: { cpuHistory: number[]; memHistory: number[] }) {
  const { snapshot, refresh, busy, setBusy, toast, navigate, openAuth, openWizard } = useApp();
  const { connection, worker, agents, system, tasks, events, operator } = snapshot;
  const [actionError, setActionError] = useState("");

  const usable = agents.filter((agent) => agent.availability === "usable").length;
  const installed = agents.filter((agent) => agent.installed).length;
  const activeTasks = tasks.filter((task) => ["running", "assigned", "queued", "awaiting_approval"].includes(task.status || ""));
  const connected = connection.state === "connected";

  const run = async (name: string, action: () => Promise<unknown>, message: string, tone: "ok" | "warn" | "bad" = "ok") => {
    setBusy(name);
    setActionError("");
    try {
      await action();
      toast(message, tone);
      await refresh();
    } catch (error: unknown) {
      const text = (error as Error).message;
      setActionError(text);
      toast(text, "bad");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="page dashboard">
      <StatusBanner
        state={connected ? "ok" : connection.paired ? "warn" : "bad"}
        title={connectionLabel(connection.state)}
        detail={
          connected
            ? `${connection.worker_name} is talking to ${connection.server_url}${connection.latency_ms != null ? ` · ${connection.latency_ms} ms` : ""}.`
            : connection.error || connection.insecure_warning || "This laptop is not currently connected to the mesh."
        }
      >
        <div className="banner-actions">
          <Button size="sm" variant="secondary" icon="wifi" onClick={() => run("test", api.connectionTest, "Connection tested against the real server.")} disabled={busy === "test" || !connection.paired}>
            Test
          </Button>
          <Button size="sm" variant="ghost" icon="refresh" onClick={() => run("restart", () => api.restartWorker(false), "Worker restarted.", "warn")} disabled={busy === "restart" || !connection.paired}>
            Restart worker
          </Button>
        </div>
      </StatusBanner>

      {actionError && (
        <div className="inline-alert bad">
          <Icon name="alert" size={17} />
          <span>{actionError}</span>
        </div>
      )}

      <div className="stat-grid">
        <article className="stat">
          <div className="stat-head">
            <span>Connection</span>
            <Icon name="wifi" size={17} />
          </div>
          <strong className={connected ? "ok" : "warn"}>{connected ? "Online" : connection.paired ? "Offline" : "Unpaired"}</strong>
          <span className="stat-detail">
            {connection.paired ? `${connection.worker_id} · token ${connection.token_fingerprint}` : "No worker token yet"}
          </span>
          <div className="stat-foot">
            <span>Last hello {timeAgo(connection.last_hello_at)}</span>
            <span>{connection.latency_ms != null ? `${connection.latency_ms} ms` : "—"}</span>
          </div>
        </article>

        <article className="stat">
          <div className="stat-head">
            <span>Worker process</span>
            <Icon name="activity" size={17} />
          </div>
          <strong>{worker.status === "connected" ? "Supervised" : worker.status}</strong>
          <span className="stat-detail">
            {worker.pid ? `PID ${worker.pid} · ${worker.owner}` : worker.unit_active ? "Owned by systemd" : "No local worker process"}
          </span>
          <div className="stat-foot">
            <span>Uptime {uptime(worker.started_at)}</span>
            <span>{activeTasks.length} active</span>
          </div>
        </article>

        <article className="stat">
          <div className="stat-head">
            <span>Local agents</span>
            <Icon name="agent" size={17} />
          </div>
          <strong>{usable}/2 usable</strong>
          <span className="stat-detail">{installed} installed · verification is a real CLI run</span>
          <div className="stat-foot">
            {agents.map((agent) => (
              <span key={agent.kind} className="agent-dot">
                <i className={agent.availability} /> {agent.display_name}
              </span>
            ))}
          </div>
        </article>

        <article className="stat">
          <div className="stat-head">
            <span>System pressure</span>
            <Icon name="cpu" size={17} />
          </div>
          <strong>{system.cpu_percent != null ? `${system.cpu_percent.toFixed(0)}%` : "—"}</strong>
          <span className="stat-detail">
            {formatBytes(system.mem_used_bytes)} / {formatBytes(system.mem_total_bytes)} RAM
            {system.mem_percent != null ? ` (${system.mem_percent.toFixed(0)}%)` : ""}
          </span>
          <Sparkline values={cpuHistory} tone="violet" />
          <div className="mem-spark">
            <span>RAM</span>
            <Sparkline values={memHistory} tone="green" />
          </div>
        </article>
      </div>

      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <div>
              <h2>Live work</h2>
              <p>Merged server tasks and this worker's own execution state.</p>
            </div>
            <Button size="sm" variant="ghost" icon="arrow" onClick={() => navigate("tasks")}>All tasks</Button>
          </div>
          {activeTasks.length === 0 ? (
            <Empty
              title="No task is running"
              detail={connection.paired ? "The worker is idle. It will claim queued work as the server publishes it." : "Pair this laptop to start claiming work."}
            />
          ) : (
            <div className="task-list">
              {activeTasks.slice(0, 5).map((task) => (
                <button key={task.id} className="task-row" onClick={() => navigate("tasks")}>
                  <div className="task-row-top">
                    <span className="task-title">{task.title || task.id}</span>
                    <Pill tone={taskStatusTone(task.status)}>{taskStatusLabel(task.status)}</Pill>
                  </div>
                  <div className="task-row-meta">
                    <span className="mono">{task.id}</span>
                    <span>{task.worker_name || task.worker_id || "unassigned"}</span>
                    <span>{task.updated_at ? timeAgo(task.updated_at) : ""}</span>
                  </div>
                  <Progress value={task.progress ?? (task.status === "running" ? 35 : 0)} />
                  {task.message && <span className="task-msg">{task.message}</span>}
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="card">
          <div className="card-head">
            <div>
              <h2>Agent readiness</h2>
              <p>Roster is fixed to the two documented CLIs.</p>
            </div>
            <Button size="sm" variant="ghost" icon="arrow" onClick={() => navigate("agents")}>Manage</Button>
          </div>
          <div className="agent-mini-list">
            {agents.map((agent) => (
              <div key={agent.kind} className="agent-mini">
                <div className={`agent-glyph ${agent.kind}`}>
                  {agent.kind === "opencode" ? "OC" : "Q"}
                </div>
                <div className="agent-mini-copy">
                  <div className="agent-mini-top">
                    <strong>{agent.display_name}</strong>
                    <Pill tone={availabilityTone(agent.availability)}>{agent.availability}</Pill>
                  </div>
                  <span className="mono small">{agent.binary}{agent.version ? ` · ${agent.version}` : ""}</span>
                  <span className="small muted ellipsis">{agent.auth_detail || (agent.installed ? "Installed; sign-in not checked" : "Not installed")}</span>
                </div>
                <div className="agent-mini-actions">
                  {!agent.installed ? (
                    <Button size="sm" variant="secondary" icon="download" onClick={() => openWizard({ kind: agent.kind, operation: "install" })}>Install</Button>
                  ) : (
                    <Button size="sm" variant="ghost" icon="terminal" onClick={() => openAuth({ kind: agent.kind })}>Sign in</Button>
                  )}
                </div>
              </div>
            ))}
          </div>
          <div className="agent-footnote">
            <Icon name="info" size={15} />
            <span>“Usable” is only set after a real functional CLI run, never from version output alone.</span>
          </div>
        </section>
      </div>

      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <div>
              <h2>Recent local events</h2>
              <p>What this bridge and worker actually did.</p>
            </div>
            <Button size="sm" variant="ghost" icon="arrow" onClick={() => navigate("activity")}>Timeline</Button>
          </div>
          {events.length === 0 ? (
            <Empty title="No events yet" detail="Pairing, worker starts, agent installs and task progress appear here." />
          ) : (
            <ul className="event-list">
              {events.slice(-7).reverse().map((event) => (
                <li key={event.id}>
                  <span className={`event-dot ${event.level}`} />
                  <div>
                    <strong>{event.message}</strong>
                    <span>
                      {event.kind} · {timeAgo(event.at)}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card">
          <div className="card-head">
            <div>
              <h2>Server view</h2>
              <p>Counts read with optional operator mode.</p>
            </div>
            {operator.available ? (
              <Pill tone="green">Operator mode</Pill>
            ) : (
              <Button size="sm" variant="secondary" icon="shield" onClick={() => navigate("settings")}>Enable</Button>
            )}
          </div>
          {operator.available ? (
            <>
              <div className="mini-stats">
                <div><strong>{operator.counts.projects ?? 0}</strong><span>projects</span></div>
                <div><strong>{operator.counts.tasks ?? 0}</strong><span>tasks</span></div>
                <div><strong>{operator.counts.workers ?? 0}</strong><span>workers</span></div>
                <div><strong>{operator.counts.documents ?? 0}</strong><span>documents</span></div>
              </div>
              <div className="operator-recent">
                {operator.projects.slice(0, 4).map((project) => (
                  <button key={project.id} className="operator-row" onClick={() => navigate("review")}>
                    <div>
                      <strong>{project.name}</strong>
                      <span className="mono small">{project.id}</span>
                    </div>
                    <Pill tone={project.status === "completed" ? "green" : "blue"}>{project.status}</Pill>
                    <span className="small muted">{project.progress ?? 0}%</span>
                  </button>
                ))}
                {operator.projects.length === 0 && (
                  <Empty title="No server projects" detail="Create a project on the control server to review it here." />
                )}
              </div>
              {operator.error && <p className="small bad">Operator refresh: {operator.error}</p>}
              <p className="small muted">Last read {timeAgo(operator.last_refresh)}. Read-only until you press an action.</p>
            </>
          ) : (
            <div className="operator-locked">
              <Icon name="shield" size={24} />
              <h3>Worker token cannot read project data</h3>
              <p>
                The mesh worker token is scoped to the worker socket. Projects, approvals, tasks and
                delivery routes require the server control token. Enable operator mode deliberately in
                Settings; it is stored separately and never sent to the worker.
              </p>
              <Button size="sm" variant="secondary" icon="settings" onClick={() => navigate("settings")}>Open settings</Button>
            </div>
          )}
        </section>
      </div>

      <section className="card">
        <div className="card-head">
          <div>
            <h2>Supervision</h2>
            <p>Start, stop and inspect the real worker without leaving the laptop.</p>
          </div>
        </div>
        <div className="quick-grid">
          <div className="quick">
            <span className="quick-icon"><Icon name="play" size={18} /></span>
            <div><strong>Start worker</strong><span>Launch the supervised Python loop.</span></div>
            <Button size="sm" disabled={Boolean(worker.pid) || busy === "start"} onClick={() => run("start", () => api.startWorker(false), "Worker started.")}>Start</Button>
          </div>
          <div className="quick">
            <span className="quick-icon"><Icon name="stop" size={18} /></span>
            <div><strong>Stop worker</strong><span>Stop only this GUI-owned process.</span></div>
            <Button size="sm" variant="secondary" disabled={!worker.pid || busy === "stop"} onClick={() => run("stop", () => api.stopWorker(false), "Worker stopped.", "warn")}>Stop</Button>
          </div>
          <div className="quick">
            <span className="quick-icon"><Icon name="refresh" size={18} /></span>
            <div><strong>Restart worker</strong><span>Stop, then start with the same token.</span></div>
            <Button size="sm" variant="secondary" disabled={!connection.paired || busy === "restart"} onClick={() => run("restart", () => api.restartWorker(false), "Worker restarted.", "warn")}>Restart</Button>
          </div>
          <div className="quick">
            <span className="quick-icon"><Icon name="server" size={18} /></span>
            <div><strong>Report inventory</strong><span>POST /api/mesh/inventory only.</span></div>
            <Button size="sm" variant="secondary" disabled={!connection.paired || busy === "inventory"} onClick={() => run("inventory", api.reportInventory, "Inventory reported to the server.")}>Report</Button>
          </div>
        </div>
        <dl className="kv wide">
          <div><dt>Started</dt><dd>{formatDate(worker.started_at)}</dd></div>
          <div><dt>Last heartbeat</dt><dd>{formatDate(worker.last_heartbeat_at)}</dd></div>
          <div><dt>Reconnect</dt><dd>{worker.reconnect ? `attempt ${worker.reconnect.attempt ?? "—"} in ${worker.reconnect.delay_seconds ?? "—"}s` : "none"}</dd></div>
          <div><dt>Pending claims</dt><dd>{worker.tasks_pending}</dd></div>
        </dl>
      </section>

      <section className="card">
        <div className="card-head">
          <div>
            <h2>Worker log tail</h2>
            <p>Redacted stdout/stderr from the supervised child.</p>
          </div>
          <Button size="sm" variant="ghost" icon="external" onClick={async () => { await shell.openLog(); }}>Open log file</Button>
        </div>
        <div className="log-view small">
          {worker.logs.length === 0 && <span className="muted">No worker output yet.</span>}
          {worker.logs.slice(-40).map((line, index) => (
            <div key={index}>
              <time>{new Date(line.at).toLocaleTimeString()}</time>
              <span className={line.level}>{line.text}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
