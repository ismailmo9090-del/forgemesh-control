import { useEffect, useState, type ReactNode } from "react";
import type {
  AgentInfo,
  ArtifactRecord,
  BrowserAccount,
  DeliveryRun,
  DesktopAutostart,
  DesktopInfo,
  CouncilSummary,
  DocumentRecord,
  EventRecord,
  GitEntry,
  PairingCode,
  Project,
  ProjectDetail,
  SchedulerSnapshot,
  ServerStatus,
  TaskRecord,
  TunnelStatus,
  Worker,
  WorkerSnapshot,
  WorkspaceStatus
} from "./types";
import { api, formatBytes, formatTime } from "./api";
import { AgentCard } from "./components/AgentCard";
import { Card, Empty, Modal, PROVIDER_META, Sparkline, StatusChip } from "./components/ui";

type Action = (kind: string, action: "install" | "verify" | "detect" | "reinstall") => void;

export function Overview({
  status,
  projects,
  tasks,
  accounts,
  events,
  council,
  busy,
  onAction,
  onManage,
  onOpenLogs,
  onOpenWizard,
  onNavigate
}: {
  status: ServerStatus;
  projects: Project[];
  tasks: TaskRecord[];
  accounts: BrowserAccount[];
  events: EventRecord[];
  council: CouncilSummary | null;
  busy: Record<string, string>;
  onAction: Action;
  onManage: (kind: string) => void;
  onOpenLogs: (kind: string) => void;
  onOpenWizard: () => void;
  onNavigate: (page: string) => void;
}) {
  const metrics = status.metrics;
  const history = metrics.history?.length ? metrics.history : [metrics];
  const cpuHistory = history.map((item) => item.cpu_percent);
  const ramHistory = history.map((item) => item.ram_percent);
  const diskHistory = history.map((item) => item.disk_percent);
  const netHistory = history.map((item) => item.rx_kbps + item.tx_kbps);

  return (
    <div className="overview-grid">
      <Card
        title="Server Overview"
        subtitle="Real topology from paired workers and the central server"
        live
        className="span-2-row1"
        action={
          <button className="link-btn" onClick={onOpenWizard}>
            {status.setup.first_run_complete ? "Setup details" : "Resume setup"} →
          </button>
        }
      >
        <div className="topology">
          <div className="central-node">
            <div className="mesh-mark" aria-hidden>
              <i />
              <i />
              <i />
              <i />
            </div>
            <div>
              <strong>FORGEMESH</strong>
              <span>CENTRAL SERVER</span>
              <small>{String(status.host.hostname || "this machine")}</small>
            </div>
          </div>
          <div className="central-agents">
            {status.agents.map((agent) => (
              <div key={agent.kind} className={`central-agent ${agent.availability}`}>
                <span>{agent.kind === "opencode" ? "OC" : "QD"}</span>
                <div>
                  <strong>{agent.display_name}</strong>
                  <small>
                    {agent.installed ? `v${agent.version || "?"}` : "not installed"} ·{" "}
                    {agent.availability}
                  </small>
                </div>
                <StatusChip status={agent.availability} />
              </div>
            ))}
          </div>
          <div className="worker-line" aria-hidden />
          {status.workers.length === 0 ? (
            <div className="no-workers">
              <strong>No worker nodes paired</strong>
              <span>
                Worker topology appears here only after a Mesh Client pairs and sends a real
                heartbeat. Nothing is simulated.
              </span>
            </div>
          ) : (
            <div className="worker-grid">
              {status.workers.map((worker) => (
                <WorkerNode key={worker.id} worker={worker} />
              ))}
            </div>
          )}
        </div>
      </Card>

      <Card
        title="AI Council"
        subtitle="Provider accounts connected through ForgeMesh Browser"
        className="council-card"
        action={
          <button className="link-btn" onClick={() => onNavigate("council")}>
            Open Council →
          </button>
        }
      >
        <div className="provider-row">
          {Object.entries(PROVIDER_META).map(([key, meta]) => {
            const account = accounts.find(
              (item) => (item.provider || item.provider_id || "").toLowerCase() === key
            );
            const online = account?.status === "available";
            return (
              <div key={key} className={`provider ${online ? "online" : ""}`}>
                <span style={{ color: meta.color }}>{meta.glyph}</span>
                <strong>{meta.label}</strong>
                <small>{online ? "Online" : account ? String(account.status) : "Not configured"}</small>
              </div>
            );
          })}
        </div>
        <div className="council-status">
          <div>
            <span className="muted small">Running meetings</span>
            <strong>{council ? council.running : "—"}</strong>
          </div>
          <div>
            <span className="muted small">Last meeting</span>
            <strong>
              {council?.last
                ? `${council.last.status} · ${council.last.current_round}/${council.last.rounds_total}`
                : "none yet"}
            </strong>
          </div>
          <div>
            <span className="muted small">Providers available</span>
            <strong>
              {accounts.filter((item) => item.status === "available").length}/{Object.keys(PROVIDER_META).length}
            </strong>
          </div>
        </div>
        <div className="council-note">
          A meeting can only start when at least one provider account reports{" "}
          <code>available</code> from the browser runtime. ForgeMesh will not invent Council
          responses.
        </div>
      </Card>

      <Card title="System Health" subtitle="Linux /proc samples" className="health-card">
        <HealthRow label="CPU usage" value={`${metrics.cpu_percent.toFixed(0)}%`} values={cpuHistory} />
        <HealthRow label="RAM usage" value={`${metrics.ram_percent.toFixed(0)}%`} values={ramHistory} />
        <HealthRow label="Disk usage" value={`${metrics.disk_percent.toFixed(0)}%`} values={diskHistory} />
        <HealthRow
          label="Network"
          value={`↑ ${metrics.tx_kbps.toFixed(1)} ↓ ${metrics.rx_kbps.toFixed(1)} KiB/s`}
          values={netHistory}
          color="var(--info)"
        />
        <div className="health-facts">
          <div>
            <span>Memory</span>
            <strong>
              {formatBytes(metrics.ram_used_bytes)} / {formatBytes(metrics.ram_total_bytes)}
            </strong>
          </div>
          <div>
            <span>Disk free</span>
            <strong>{formatBytes(metrics.disk_free_bytes)}</strong>
          </div>
          <div>
            <span>Browser runtime</span>
            <StatusChip
              status={status.browser.connected ? "online" : "offline"}
              label={status.browser.connected ? "Connected" : "Unavailable"}
            />
          </div>
        </div>
      </Card>

      <Card
        title="Active Projects"
        subtitle={`${status.counts.projects} project(s)`}
        className="projects-card"
        action={
          <button className="link-btn" onClick={() => onNavigate("projects")}>
            View all →
          </button>
        }
      >
        {projects.length === 0 ? (
          <Empty
            title="No projects yet"
            detail="Create a project to start a real workspace. Nothing is seeded for show."
          />
        ) : (
          <div className="project-list">
            {projects.slice(0, 4).map((project) => (
              <div key={project.id} className="project-row">
                <div className={`project-icon ${project.mode}`}>{project.mode === "hackathon" ? "★" : "◆"}</div>
                <div className="project-main">
                  <div className="row spread">
                    <strong>{project.name}</strong>
                    <span className="mono small">{project.progress}%</span>
                  </div>
                  <span className="muted small">
                    {project.status} · {project.tasks_total} task(s)
                  </span>
                  <div className="progress">
                    <i style={{ width: `${project.progress}%` }} />
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card
        title="Task Execution"
        subtitle={`${status.counts.tasks_running} running · ${status.counts.tasks} total`}
        className="tasks-card"
        action={
          <button className="link-btn" onClick={() => onNavigate("tasks")}>
            View all →
          </button>
        }
      >
        {tasks.length === 0 ? (
          <Empty
            title="No tasks in the queue"
            detail="Tasks appear after the Council and approval gates produce a real plan."
          />
        ) : (
          <div className="task-table">
            <div className="task-head">
              <span>Task</span>
              <span>Executor</span>
              <span>Agent</span>
              <span>Status</span>
            </div>
            {tasks.slice(0, 6).map((task) => (
              <div key={task.id} className="task-row">
                <span>{task.title}</span>
                <span className="muted">{task.worker_name}</span>
                <span className="muted">{task.agent_kind || "—"}</span>
                <StatusChip status={task.status} />
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Agent Readiness" subtitle="Central coding CLIs (real verification)" className="agents-mini-card">
        <div className="mini-agents">
          {status.agents.map((agent) => (
            <AgentCard
              key={agent.kind}
              agent={agent}
              busy={busy[agent.kind]}
              onAction={(action) => onAction(agent.kind, action)}
              onLogs={() => onOpenLogs(agent.kind)}
              onManage={() => onManage(agent.kind)}
              compact
            />
          ))}
        </div>
      </Card>

      <Card
        title="Terminal"
        subtitle="Durable control-center events (not fabricated logs)"
        className="terminal-card"
        action={
          <button className="link-btn" onClick={() => onNavigate("terminal")}>
            Expand →
          </button>
        }
      >
        <div className="terminal">
          {events.slice(-12).map((event) => (
            <div key={event.id} className="terminal-line">
              <span className="terminal-time">{formatTime(event.timestamp)}</span>
              <span className="terminal-type">{event.type}</span>
              <span className="terminal-data">{summariseEvent(event)}</span>
            </div>
          ))}
          {events.length === 0 && <div className="terminal-line muted">No events recorded yet.</div>}
        </div>
      </Card>
    </div>
  );
}

function HealthRow({
  label,
  value,
  values,
  color
}: {
  label: string;
  value: string;
  values: number[];
  color?: string;
}) {
  return (
    <div className="health-row">
      <div className="row spread">
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
      <Sparkline values={values} color={color} height={38} />
    </div>
  );
}

function WorkerNode({ worker }: { worker: Worker }) {
  const inventory = worker.inventory || {};
  const cpu = typeof inventory.cpu_percent === "number" ? `${inventory.cpu_percent.toFixed(0)}%` : null;
  const ram = typeof inventory.ram_percent === "number" ? `${inventory.ram_percent.toFixed(0)}%` : null;
  return (
    <div className={`worker-node ${worker.status}`}>
      <header>
        <span className="os-glyph">{worker.os_family === "windows" ? "⊞" : "◉"}</span>
        <div>
          <strong>{worker.name}</strong>
          <small>
            {worker.os_family} {worker.os_version} · {worker.arch}
          </small>
        </div>
      </header>
      <StatusChip status={worker.status} />
      <div className="worker-meta">
        <span>CPU {cpu ?? "no telemetry"}</span>
        <span>RAM {ram ?? "no telemetry"}</span>
      </div>
      <small className="muted">
        {worker.last_heartbeat_at
          ? `Heartbeat ${formatTime(worker.last_heartbeat_at)}`
          : "Never paired"}
      </small>
    </div>
  );
}

function summariseEvent(event: EventRecord): string {
  const data = event.data || {};
  const preferred = ["message", "error", "kind", "stage", "path", "version", "name", "project_id", "state"];
  for (const key of preferred) {
    if (data[key]) return `${key}: ${String(data[key])}`;
  }
  return JSON.stringify(data).slice(0, 180);
}

export function AgentsPage({
  status,
  busy,
  onAction,
  onManage,
  onOpenLogs,
  onDetectAll
}: {
  status: ServerStatus;
  busy: Record<string, string>;
  onAction: Action;
  onManage: (kind: string) => void;
  onOpenLogs: (kind: string) => void;
  onDetectAll: () => void;
}) {
  return (
    <div className="page-stack">
      <header className="page-head">
        <div>
          <h1>AI Agents &amp; Models</h1>
          <p>Central-server CLIs only. Worker agents are reported by the Mesh Client and never installed here.</p>
        </div>
        <button className="btn" onClick={onDetectAll}>
          Detect installed CLIs
        </button>
      </header>
      <div className="agent-grid">
        {status.agents.map((agent) => (
          <AgentCard
            key={agent.kind}
            agent={agent}
            busy={busy[agent.kind]}
            onAction={(action) => onAction(agent.kind, action)}
            onLogs={() => onOpenLogs(agent.kind)}
            onManage={() => onManage(agent.kind)}
          />
        ))}
      </div>
      <Card title="How readiness is decided" subtitle="No executable equals no agent">
        <div className="explain-grid">
          <div>
            <strong>1 · Executable</strong>
            <span>Official installer runs; the resulting binary is detected and `--version` is executed.</span>
          </div>
          <div>
            <strong>2 · Interface</strong>
            <span>The CLI's own help output is parsed; ForgeMesh never invents flags.</span>
          </div>
          <div>
            <strong>3 · Real prompt</strong>
            <span>A harmless prompt is executed. Auth failures are recorded separately.</span>
          </div>
          <div>
            <strong>4 · Authentication</strong>
            <span>Official login commands only. No password, token or cookie is requested here.</span>
          </div>
        </div>
      </Card>
    </div>
  );
}

export function AgentDetail({
  agent,
  busy,
  onAction,
  onLogs,
  onClose
}: {
  agent: AgentInfo;
  busy?: string;
  onAction: (action: "verify" | "reinstall" | "install") => void;
  onLogs: () => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState(agent.path);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState<"" | "reinstall" | "update">("");

  async function savePath() {
    setSaving(true);
    setMessage("");
    try {
      await api(`/api/agents/${agent.kind}/path`, {
        method: "POST",
        body: JSON.stringify({ path })
      });
      setMessage("Path saved and verified with a real --version call.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled() {
    try {
      await api(`/api/agents/${agent.kind}/enabled`, {
        method: "POST",
        body: JSON.stringify({ enabled: !agent.enabled })
      });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  }

  async function runUpdate() {
    setConfirming("");
    setSaving(true);
    try {
      await api(`/api/agents/${agent.kind}/update`, {
        method: "POST",
        body: JSON.stringify({ approve: true })
      });
      setMessage("Update completed. Version and verification were read from the CLI again.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="agent-detail">
      <header className="row spread">
        <div>
          <h2>{agent.display_name}</h2>
          <span className="muted">
            {agent.installed ? `v${agent.version}` : "not installed"} · {agent.path || "no executable"}
          </span>
        </div>
        <button className="icon-btn" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>

      <div className="detail-grid">
        <div>
          <span>Installation status</span>
          <StatusChip status={agent.install_status} />
        </div>
        <div>
          <span>Configuration</span>
          <StatusChip status={agent.configuration_status} tone={agent.configuration_status === "configured" ? "ok" : "muted"} />
        </div>
        <div>
          <span>Authentication</span>
          <StatusChip
            status={agent.auth_status}
            tone={
              agent.auth_status === "unauthenticated" &&
              agent.metadata.auth_required_for_use === false
                ? "info"
                : undefined
            }
            label={
              agent.auth_status === "unauthenticated" &&
              agent.metadata.auth_required_for_use === false
                ? "sign-in optional"
                : undefined
            }
          />
        </div>
        <div>
          <span>Availability</span>
          <StatusChip
            status={agent.availability}
            label={
              agent.availability === "limited" &&
              agent.metadata?.functional_check?.attempted === false
                ? "unverified"
                : undefined
            }
          />
        </div>
        <div>
          <span>Update policy</span>
          <strong>{agent.update_policy}</strong>
        </div>
        <div>
          <span>Last verified</span>
          <strong>{agent.verified_at ? formatTime(agent.verified_at) : "never"}</strong>
        </div>
        <div>
          <span>Detected</span>
          <strong>{agent.detected_at ? formatTime(agent.detected_at) : "never"}</strong>
        </div>
        <div>
          <span>Last installed</span>
          <strong>{agent.last_install_at ? formatTime(agent.last_install_at) : "never"}</strong>
        </div>
      </div>

      {agent.last_error && <p className="error-box">{agent.last_error}</p>}

      <label className="field">
        <span>Executable path</span>
        <div className="row">
          <input value={path} onChange={(event) => setPath(event.target.value)} spellCheck={false} />
          <button className="btn ghost" onClick={savePath} disabled={saving || path === agent.path}>
            Verify &amp; save
          </button>
        </div>
        <small>Qoder's desktop application at /opt/Qoder is not accepted; use `qodercli`.</small>
      </label>
      {message && <p className="info-box">{message}</p>}

      <div className="detail-actions">
        <button className="btn" disabled={!!busy} onClick={() => onAction("verify")}>
          Check version &amp; verify
        </button>
        <button className="btn ghost" disabled={!!busy} onClick={onLogs}>
          View installation logs
        </button>
        <button className="btn ghost" disabled={saving} onClick={() => setConfirming("update")}>
          Update…
        </button>
        <button className="btn ghost" disabled={saving} onClick={() => setConfirming("reinstall")}>
          Reinstall…
        </button>
        <button className="btn ghost" onClick={toggleEnabled}>
          {agent.enabled ? "Disable agent" : "Enable agent"}
        </button>
      </div>

      {confirming && (
        <div className="confirm-box">
          <strong>
            {confirming === "update"
              ? `Update ${agent.display_name} using its official update command?`
              : `Reinstall ${agent.display_name} from the official source?`}
          </strong>
          <p>
            This runs a real installation command on the central server. Existing CLI configuration
            is preserved where the official installer supports it; ForgeMesh never silently updates.
          </p>
          <div className="row">
            <button
              className="btn primary"
              onClick={() =>
                confirming === "update" ? runUpdate() : (setConfirming(""), onAction("reinstall"))
              }
            >
              Confirm
            </button>
            <button className="btn ghost" onClick={() => setConfirming("")}>
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="detail-columns">
        <div>
          <h3>Detected interface</h3>
          <pre className="json-box">
            {JSON.stringify(agent.metadata?.capabilities || {}, null, 2)}
          </pre>
        </div>
        <div>
          <h3>Models reported by the CLI</h3>
          {agent.metadata?.models?.length ? (
            <ul className="model-list">
              {agent.metadata.models.slice(0, 20).map((model) => (
                <li key={model}>{model}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">
              {agent.auth_status === "unauthenticated" &&
              agent.metadata?.auth_required_for_use !== false
                ? "No models listed until the official login is completed."
                : agent.metadata?.auth_required_for_use === false
                  ? "The CLI reported no models; its free models can still run without a credential."
                  : "The CLI reported no models."}
            </p>
          )}
        </div>
      </div>

      <p className="muted small">{agent.metadata?.auth_detail || ""}</p>
    </div>
  );
}

export function ProjectsPage({
  projects,
  onCreated
}: {
  projects: Project[];
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [mode, setMode] = useState("normal");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [openId, setOpenId] = useState("");

  async function create() {
    setSaving(true);
    setError("");
    try {
      await api("/api/projects", {
        method: "POST",
        body: JSON.stringify({ name, description, mode })
      });
      setName("");
      setDescription("");
      onCreated();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page-stack">
      <header className="page-head">
        <div>
          <h1>Projects</h1>
          <p>Each project gets a real workspace directory. Submissions are never pre-filled.</p>
        </div>
      </header>
      <Card title="Create project" subtitle="Normal or Hackathon mode">
        <div className="form-grid">
          <label className="field">
            <span>Name</span>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Project name" />
          </label>
          <label className="field">
            <span>Mode</span>
            <select value={mode} onChange={(event) => setMode(event.target.value)}>
              <option value="normal">Normal project mode</option>
              <option value="hackathon">Hackathon mode</option>
            </select>
          </label>
          <label className="field wide">
            <span>Description</span>
            <textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="What are we building?"
              rows={3}
            />
          </label>
        </div>
        {error && <p className="error-box">{error}</p>}
        <button className="btn primary" onClick={create} disabled={!name.trim() || saving}>
          {saving ? "Creating…" : "Create project"}
        </button>
      </Card>

      <Card title="All projects" subtitle={`${projects.length} real project(s)`}>
        {projects.length === 0 ? (
          <Empty title="No projects" detail="Create the first project above." />
        ) : (
          <div className="project-table">
            {projects.map((project) => (
              <div key={project.id} className="project-table-row">
                <div>
                  <strong>{project.name}</strong>
                  <small className="muted mono">{project.id}</small>
                </div>
                <StatusChip status={project.status} />
                <span>{project.mode}</span>
                <span>
                  {project.tasks_completed}/{project.tasks_total} tasks
                </span>
                <div className="progress thin">
                  <i style={{ width: `${project.progress}%` }} />
                </div>
                <span className="mono small">{project.progress}%</span>
                <span className="muted small ellipsis" title={project.workspace_path}>
                  {project.workspace_path}
                </span>
                <button className="btn ghost small-btn" onClick={() => setOpenId(project.id)}>
                  Gates
                </button>
              </div>
            ))}
          </div>
        )}
      </Card>

      {openId && (
        <Modal title="Project gates & approvals" onClose={() => setOpenId("")} width="980px">
          <ProjectDetailPanel
            projectId={openId}
            onChanged={onCreated}
            onClose={() => setOpenId("")}
          />
        </Modal>
      )}
    </div>
  );
}

function ProjectDetailPanel({
  projectId,
  onChanged,
  onClose
}: {
  projectId: string;
  onChanged: () => void;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceStatus | null>(null);
  const [commits, setCommits] = useState<GitEntry[]>([]);
  const [runs, setRuns] = useState<DeliveryRun[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactRecord[]>([]);
  const [command, setCommand] = useState("git status --short");
  const [runOutput, setRunOutput] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [notes, setNotes] = useState<Record<string, string>>({});

  const load = async () => {
    try {
      const next = await api<ProjectDetail>(`/api/projects/${projectId}`);
      setDetail(next);
      setError("");
      const path = next.workspace_path;
      const results = await Promise.allSettled([
        api<WorkspaceStatus>(`/api/workspaces/status?path=${encodeURIComponent(path)}`),
        api<{ entries: GitEntry[] }>(`/api/workspaces/log?path=${encodeURIComponent(path)}&limit=10`),
        api<{ runs: DeliveryRun[] }>(`/api/delivery/runs?project_id=${encodeURIComponent(projectId)}&limit=10`),
        api<{ artifacts: ArtifactRecord[] }>(
          `/api/delivery/artifacts?project_id=${encodeURIComponent(projectId)}`
        )
      ]);
      if (results[0].status === "fulfilled") setWorkspace(results[0].value);
      if (results[1].status === "fulfilled") setCommits(results[1].value.entries);
      if (results[2].status === "fulfilled") setRuns(results[2].value.runs);
      if (results[3].status === "fulfilled") setArtifacts(results[3].value.artifacts);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    }
  };

  useEffect(() => {
    void load();
  }, [projectId]);

  async function workspaceAction(path: string, body: Record<string, unknown>, label: string) {
    setBusy(label);
    setError("");
    setRunOutput("");
    try {
      const result = await api<Record<string, unknown>>(path, {
        method: "POST",
        body: JSON.stringify(body)
      });
      setRunOutput(JSON.stringify(result, null, 2));
      await load();
      onChanged();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy("");
    }
  }

  async function decide(approvalId: string, approved: boolean) {
    setBusy(approvalId);
    setError("");
    try {
      await api(`/api/projects/${projectId}/approvals/${approvalId}/decide`, {
        method: "POST",
        body: JSON.stringify({ approved, note: notes[approvalId] || "" })
      });
      setNotes((previous) => {
        const next = { ...previous };
        delete next[approvalId];
        return next;
      });
      await load();
      onChanged();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy("");
    }
  }

  async function resolveAction(actionId: string) {
    setBusy(actionId);
    setError("");
    try {
      await api(`/api/projects/${projectId}/human-actions/${actionId}/resolve`, {
        method: "POST",
        body: JSON.stringify({
          status: "done",
          note: "Marked resolved from the dashboard."
        })
      });
      await load();
      onChanged();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy("");
    }
  }

  if (!detail) {
    return <p className="muted">{error || "Reading project…"}</p>;
  }

  const pending = detail.approvals.filter((approval) => approval.status === "pending");
  const decided = detail.approvals.filter((approval) => approval.status !== "pending");

  return (
    <div className="stack">
      {error && <p className="error-box">{error}</p>}
      <div className="detail-grid">
        <div>
          <span>Project</span>
          <strong>{detail.name}</strong>
        </div>
        <div>
          <span>Mode</span>
          <strong>{detail.mode}</strong>
        </div>
        <div>
          <span>Status</span>
          <StatusChip status={detail.status} />
        </div>
        <div>
          <span>Workspace</span>
          <strong className="mono small ellipsis">{detail.workspace_path}</strong>
        </div>
      </div>

      <section>
        <h3>Pending approvals ({pending.length})</h3>
        {pending.length === 0 ? (
          <p className="muted small">
            Nothing is waiting for a human decision. Planning stays blocked until the blueprint gate
            is approved.
          </p>
        ) : (
          pending.map((approval) => (
            <div key={approval.id} className="approval-box">
              <header>
                <div>
                  <strong>{approval.stage.replace("_", " ")}</strong>
                  <small className="muted"> requested {formatTime(approval.requested_at)}</small>
                </div>
                <StatusChip status={approval.status} />
              </header>
              <p>{approval.summary || "No summary recorded."}</p>
              <pre className="json-box">{JSON.stringify(approval.details, null, 2)}</pre>
              <div className="row">
                <input
                  placeholder="Decision note (optional)"
                  value={notes[approval.id] || ""}
                  onChange={(event) =>
                    setNotes((previous) => ({ ...previous, [approval.id]: event.target.value }))
                  }
                />
                <button
                  className="btn primary"
                  disabled={busy === approval.id}
                  onClick={() => void decide(approval.id, true)}
                >
                  Approve
                </button>
                <button
                  className="btn danger"
                  disabled={busy === approval.id}
                  onClick={() => void decide(approval.id, false)}
                >
                  Reject
                </button>
              </div>
            </div>
          ))
        )}
      </section>

      <section>
        <h3>Human action checklist ({detail.human_actions.length})</h3>
        {detail.human_actions.length === 0 ? (
          <p className="muted small">
            No human action was requested for this project. Credentials are never handled here
            without one.
          </p>
        ) : (
          <div className="human-action-list">
            {detail.human_actions.map((action) => (
              <div key={action.id} className={`human-action ${action.status}`}>
                <div>
                  <strong>{action.title}</strong>
                  <small className="muted">{action.detail}</small>
                </div>
                <StatusChip status={action.status} />
                {["pending", "in_progress"].includes(action.status) && (
                  <button
                    className="btn ghost small-btn"
                    disabled={busy === action.id}
                    onClick={() => void resolveAction(action.id)}
                  >
                    Mark resolved
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h3>Blueprints</h3>
        {detail.blueprint ? (
          <pre className="json-box">{JSON.stringify(detail.blueprint, null, 2)}</pre>
        ) : (
          <p className="muted small">No blueprint has been saved for this project.</p>
        )}
        {detail.ui_spec && <pre className="json-box">{JSON.stringify(detail.ui_spec, null, 2)}</pre>}
      </section>

      <section>
        <h3>Workspace</h3>
        {workspace ? (
          <>
            <div className="detail-grid">
              <div>
                <span>Repository</span>
                <StatusChip
                  status={workspace.is_repo ? "ready" : "not_detected"}
                  label={workspace.is_repo ? "git repository" : "not a git repository"}
                />
              </div>
              <div>
                <span>Branch</span>
                <strong>{workspace.branch || "—"}</strong>
              </div>
              <div>
                <span>Working tree</span>
                <strong>{workspace.clean === null ? "unknown" : workspace.clean ? "clean" : "dirty"}</strong>
              </div>
              <div>
                <span>HEAD</span>
                <strong className="mono small">{workspace.head || "no commits"}</strong>
              </div>
            </div>
            {workspace.error && <p className="muted small">{workspace.error}</p>}
            {workspace.changes.length > 0 && (
              <pre className="json-box">{workspace.changes.join("\n")}</pre>
            )}
            {!workspace.is_repo && (
              <button
                className="btn"
                disabled={!!busy}
                onClick={() =>
                  void workspaceAction(
                    "/api/workspaces/init",
                    { path: detail.workspace_path, default_branch: "main" },
                    "init"
                  )
                }
              >
                {busy === "init" ? "Initialising…" : "Initialise git repository"}
              </button>
            )}
            {commits.length > 0 && (
              <div className="commit-list">
                {commits.map((entry) => (
                  <div key={entry.commit} className="commit-row">
                    <code className="small">{entry.commit.slice(0, 8)}</code>
                    <span className="ellipsis">{entry.subject}</span>
                    <span className="muted small">{entry.author}</span>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
          <p className="muted small">Reading workspace state…</p>
        )}

        <div className="row">
          <input
            value={command}
            onChange={(event) => setCommand(event.target.value)}
            placeholder="Allowed verification command"
            spellCheck={false}
          />
          <button
            className="btn"
            disabled={!command.trim() || !!busy}
            onClick={() =>
              void workspaceAction(
                "/api/delivery/run",
                { project_id: projectId, command: command.trim() },
                "run"
              )
            }
          >
            {busy === "run" ? "Running…" : "Run verification"}
          </button>
        </div>
        <p className="muted small">
          Only the server's verification allowlist is accepted (pytest, npm test/build/lint, npx tsc,
          git status/diff, …). The command runs in the project workspace with a real exit code.
        </p>
        {runOutput && <pre className="log-box">{runOutput}</pre>}

        <div className="detail-actions">
          <button
            className="btn ghost small-btn"
            disabled={!!busy}
            onClick={() =>
              void workspaceAction(
                "/api/delivery/artifacts",
                { project_id: projectId },
                "artifacts"
              )
            }
          >
            {busy === "artifacts" ? "Hashing…" : "Collect artifacts"}
          </button>
          <button
            className="btn ghost small-btn"
            disabled={!!busy}
            onClick={() =>
              void workspaceAction("/api/delivery/report", { project_id: projectId, kind: "summary" }, "report")
            }
          >
            {busy === "report" ? "Writing…" : "Write delivery report"}
          </button>
        </div>

        {runs.length > 0 && (
          <div className="run-list">
            {runs.map((run) => (
              <div key={run.id} className="run-row">
                <div>
                  <strong className="mono small">{run.command}</strong>
                  <small className="muted">
                    {run.id} · {run.duration_ms ?? "?"} ms
                  </small>
                </div>
                <StatusChip status={run.status} />
              </div>
            ))}
          </div>
        )}
        {artifacts.length > 0 && (
          <div className="artifact-list">
            {artifacts.map((artifact) => (
              <div key={artifact.id} className="artifact-row">
                <span className="mono small">{artifact.name}</span>
                <span className="muted small">{formatBytes(artifact.size)}</span>
                <StatusChip
                  status={artifact.verified ? "available" : "unknown"}
                  label={artifact.verified ? "hash verified" : "unverified"}
                />
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h3>Decided approvals ({decided.length})</h3>
        {decided.length === 0 ? (
          <p className="muted small">No approval has been decided yet.</p>
        ) : (
          <div className="approval-history">
            {decided.map((approval) => (
              <div key={approval.id} className="approval-history-row">
                <strong>{approval.stage.replace("_", " ")}</strong>
                <StatusChip status={approval.status} />
                <span className="muted small">
                  {approval.decided_at ? formatTime(approval.decided_at) : ""} ·{" "}
                  {approval.note || "no note"}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h3>Stored secrets ({detail.secrets.length})</h3>
        {detail.secrets.length === 0 ? (
          <p className="muted small">
            No secret is stored for this project. Values are never displayed, even when present.
          </p>
        ) : (
          <ul className="statement-list">
            {detail.secrets.map((secret) => (
              <li key={secret.name}>
                <code>{secret.name}</code> · stored {secret.updated_at ? formatTime(secret.updated_at) : "—"}
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="detail-actions">
        <button className="btn ghost" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

export function DocumentsPage({
  documents,
  onUploaded
}: {
  documents: DocumentRecord[];
  onUploaded: () => void;
}) {
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [detail, setDetail] = useState<DocumentRecord | null>(null);

  async function upload(file: File) {
    setUploading(true);
    setError("");
    const form = new FormData();
    form.append("file", file);
    try {
      const result = await api<DocumentRecord>("/api/documents", { method: "POST", body: form });
      setDetail(result);
      onUploaded();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setUploading(false);
    }
  }

  async function open(document: DocumentRecord) {
    try {
      setDetail(await api<DocumentRecord>(`/api/documents/${document.id}`));
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    }
  }

  return (
    <div className="page-stack">
      <header className="page-head">
        <div>
          <h1>Problem Statements</h1>
          <p>PDF, DOCX, TXT, Markdown, ZIP and images are extracted locally. Scanned PDFs need OCR.</p>
        </div>
        <label className={`btn primary file-btn ${uploading ? "disabled" : ""}`}>
          {uploading ? "Uploading…" : "Upload document"}
          <input
            type="file"
            hidden
            disabled={uploading}
            onChange={(event) => {
              const file = event.target.files?.[0];
              // Reset so choosing the same file twice still fires onChange.
              event.target.value = "";
              if (file && !uploading) void upload(file);
            }}
          />
        </label>
      </header>
      {error && <p className="error-box">{error}</p>}
      <div className="doc-layout">
        <Card title="Documents" subtitle={`${documents.length} stored`}>
          {documents.length === 0 ? (
            <Empty title="No documents" detail="Upload a statement to extract real text." />
          ) : (
            <div className="doc-list">
              {documents.map((document) => (
                <button key={document.id} className="doc-row" onClick={() => open(document)}>
                  <span className="doc-icon">{document.filename.split(".").pop()?.toUpperCase() || "FILE"}</span>
                  <div>
                    <strong>{document.filename}</strong>
                    <small className="muted">
                      {document.extracted?.chars ?? 0} chars · {document.status}
                    </small>
                  </div>
                  <StatusChip
                    status={document.status === "extracted" ? "ready" : document.status === "failed" ? "failed" : "idle"}
                    label={document.status}
                  />
                </button>
              ))}
            </div>
          )}
        </Card>
        <Card title="Extracted statement" subtitle={detail?.filename || "Select a document"}>
          {detail ? (
            <div className="doc-detail">
              {detail.error && <p className="error-box">{detail.error}</p>}
              <pre className="json-box">
                {JSON.stringify(detail.extracted?.fields || {}, null, 2)}
              </pre>
              {detail.statements?.length ? (
                <div>
                  <h3>{detail.statements.length} statement(s)</h3>
                  <ol className="statement-list">
                    {detail.statements.map((item, index) => (
                      <li key={index}>{JSON.stringify(item)}</li>
                    ))}
                  </ol>
                </div>
              ) : null}
              {detail.text && <pre className="text-box">{detail.text.slice(0, 12000)}</pre>}
            </div>
          ) : (
            <Empty title="Nothing selected" detail="Extracted text appears here, verbatim." />
          )}
        </Card>
      </div>
    </div>
  );
}

type TunnelAction = "start" | "stop" | "install" | "authtoken";

const TUNNEL_STATE_LABEL: Record<TunnelStatus["state"], string> = {
  not_installed: "not installed",
  needs_authtoken: "token needed",
  stopped: "stopped",
  starting: "starting",
  running: "live",
  error: "error"
};

function TunnelCard({
  tunnel,
  pairing,
  label,
  busy,
  message,
  onAction,
  onCopy,
  onCreatePairing
}: {
  tunnel: TunnelStatus | null;
  pairing: PairingCode | null;
  label: string;
  busy: string;
  message: string;
  onAction: (kind: TunnelAction, token?: string) => Promise<boolean>;
  onCopy: (value: string, what: string) => void;
  onCreatePairing: () => void;
}) {
  const [token, setToken] = useState("");
  const [showForm, setShowForm] = useState(false);

  const joinCommands =
    tunnel?.public_url && pairing
      ? [
          `fmw register --server ${tunnel.public_url} --code ${pairing.code} --name "${
            label.trim() || "Worker 1"
          }"`,
          "fmw run"
        ].join("\n")
      : "";

  return (
    <Card
      title="Public access · ngrok"
      subtitle="Expose this control server over HTTPS so Mesh Clients on other networks can connect."
      action={
        tunnel ? (
          <StatusChip
            status={
              tunnel.running ? "running" : tunnel.state === "error" ? "failed" : tunnel.state
            }
            label={TUNNEL_STATE_LABEL[tunnel.state]}
          />
        ) : undefined
      }
    >
      {!tunnel ? (
        <p className="muted small">Reading ngrok state…</p>
      ) : (
        <>
          {tunnel.installed ? (
            <p className="muted small mono">
              ngrok {tunnel.version || "installed"} · {tunnel.path}
            </p>
          ) : (
            <div className="pairing-box">
              <strong>ngrok is not installed on this server</strong>
              <p className="muted small">
                Workers never need ngrok; only this server does. Download the official agent once,
                then save your authtoken below.
              </p>
              <div className="row">
                <button
                  className="btn primary"
                  disabled={busy === "install" || !tunnel.install_supported}
                  onClick={() => void onAction("install")}
                >
                  {busy === "install" ? "Downloading…" : "Download ngrok (official)"}
                </button>
                <a
                  className="btn ghost"
                  href={tunnel.install_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  ngrok.com/download
                </a>
              </div>
              {!tunnel.install_supported && (
                <p className="muted small">
                  Automatic download is offered on Linux only. Use the link above.
                </p>
              )}
              {tunnel.install_commands[0] && (
                <code className="small">{tunnel.install_commands[0]}</code>
              )}
            </div>
          )}

          {tunnel.installed && (!tunnel.authtoken_saved || showForm) && (
            <div className="pairing-box">
              <strong>
                {tunnel.authtoken_saved ? "Replace the ngrok authtoken" : "Connect your ngrok account"}
              </strong>
              <p className="muted small">
                Create a free account at{" "}
                <a href="https://dashboard.ngrok.com/get-started/your-authtoken" target="_blank" rel="noreferrer">
                  dashboard.ngrok.com
                </a>
                , copy the authtoken and paste it here. It is saved by ngrok&apos;s own{" "}
                <code>config add-authtoken</code> command in a private file (mode 0600), and the
                dashboard never shows it again.
              </p>
              <div className="row">
                <input
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={token}
                  onChange={(event) => setToken(event.target.value)}
                  placeholder="ngrok authtoken"
                />
                <button
                  className="btn primary"
                  disabled={busy === "authtoken" || token.trim().length < 20}
                  onClick={async () => {
                    if (await onAction("authtoken", token.trim())) {
                      setToken("");
                      setShowForm(false);
                    }
                  }}
                >
                  {busy === "authtoken" ? "Saving…" : "Save authtoken"}
                </button>
                {tunnel.authtoken_saved && (
                  <button
                    className="btn ghost"
                    onClick={() => {
                      setShowForm(false);
                      setToken("");
                    }}
                  >
                    Cancel
                  </button>
                )}
              </div>
            </div>
          )}

          {tunnel.installed && tunnel.authtoken_saved && (
            <>
              <div className="detail-grid">
                <div>
                  <span>Credential</span>
                  <strong>
                    {tunnel.authtoken_source === "forgemesh"
                      ? "ForgeMesh config"
                      : "existing ngrok config"}
                  </strong>
                </div>
                <div>
                  <span>Local server</span>
                  <code className="small">{tunnel.local_url}</code>
                </div>
                <div>
                  <span>Public URL</span>
                  <strong>{tunnel.public_url || "not started"}</strong>
                </div>
                <div>
                  <span>Free plan note</span>
                  <strong>URL changes on restart</strong>
                </div>
              </div>

              <div className="row">
                {tunnel.running ? (
                  <button
                    className="btn danger"
                    disabled={busy === "stop"}
                    onClick={() => void onAction("stop")}
                  >
                    {busy === "stop" ? "Stopping…" : "Stop tunnel"}
                  </button>
                ) : (
                  <button
                    className="btn primary"
                    disabled={busy === "start" || tunnel.state === "starting"}
                    onClick={() => void onAction("start")}
                  >
                    {busy === "start" || tunnel.state === "starting"
                      ? "Starting…"
                      : "Start public tunnel"}
                  </button>
                )}
                <button className="btn ghost small-btn" onClick={() => setShowForm((value) => !value)}>
                  {showForm ? "Hide token form" : "Replace token"}
                </button>
              </div>
            </>
          )}

          {tunnel.running && tunnel.public_url && (
            <div className="pairing-box">
              <div>
                <span className="muted small">Public HTTPS URL</span>
                <strong className="mono">{tunnel.public_url}</strong>
              </div>
              <div className="row">
                <button
                  className="btn ghost small-btn"
                  onClick={() => onCopy(tunnel.public_url || "", "Public URL")}
                >
                  Copy URL
                </button>
                <a
                  className="btn ghost small-btn"
                  href={tunnel.public_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open dashboard
                </a>
                <button className="btn ghost small-btn" onClick={onCreatePairing}>
                  Create pairing code
                </button>
              </div>
              <p className="muted small">
                Anyone with this URL can reach the dashboard; the control token still protects it,
                so keep the URL private. {tunnel.free_plan_note} ngrok&apos;s free plan can show a
                one-time browser warning page — click “Visit Site”. Mesh Client WebSockets are not
                affected.
              </p>
            </div>
          )}

          {tunnel.public_url &&
            (pairing ? (
              <div>
                <span className="muted small">
                  Worker command · code valid until {formatTime(pairing.expires_at)}
                </span>
                <pre className="json-box">{joinCommands}</pre>
                <button
                  className="btn ghost small-btn"
                  onClick={() => onCopy(joinCommands, "Worker commands")}
                >
                  Copy commands
                </button>
              </div>
            ) : (
              <p className="muted small">
                Create a pairing code to get the exact Mesh Client command for this public URL.
              </p>
            ))}

          {tunnel.last_error && (
            <details className="pairing-box">
              <summary>ngrok reported an error</summary>
              <pre className="json-box">{tunnel.last_error}</pre>
            </details>
          )}

          {message && <p className="muted small">{message}</p>}
        </>
      )}
    </Card>
  );
}

export function WorkersPage() {
  const [snapshot, setSnapshot] = useState<WorkerSnapshot | null>(null);
  const [tunnel, setTunnel] = useState<TunnelStatus | null>(null);
  const [pairing, setPairing] = useState<PairingCode | null>(null);
  const [label, setLabel] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [tunnelBusy, setTunnelBusy] = useState("");
  const [tunnelMessage, setTunnelMessage] = useState("");
  const [confirmRevoke, setConfirmRevoke] = useState("");

  const load = async () => {
    try {
      const [nextSnapshot, nextTunnel] = await Promise.all([
        api<WorkerSnapshot>("/api/workers"),
        api<TunnelStatus>("/api/tunnel")
      ]);
      setSnapshot(nextSnapshot);
      setTunnel(nextTunnel);
      setError("");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, []);

  async function createPairing() {
    setBusy("pairing");
    setError("");
    try {
      setPairing(
        await api<PairingCode>("/api/workers/pairing", {
          method: "POST",
          body: JSON.stringify({ label: label.trim() })
        })
      );
      await load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy("");
    }
  }

  async function runTunnel(kind: TunnelAction, token?: string): Promise<boolean> {
    setTunnelBusy(kind);
    setTunnelMessage("");
    setError("");
    try {
      const path =
        kind === "authtoken" ? "/api/tunnel/authtoken" : `/api/tunnel/${kind}`;
      const next = await api<TunnelStatus>(path, {
        method: "POST",
        body: kind === "authtoken" ? JSON.stringify({ token }) : undefined
      });
      setTunnel(next);
      if (kind === "authtoken") {
        setTunnelMessage(
          next.restart_required
            ? "Authtoken saved. Restart the tunnel so ngrok uses it."
            : "Authtoken saved. The dashboard will not show it again."
        );
      } else if (kind === "start") {
        setTunnelMessage(
          next.public_url
            ? `Tunnel live at ${next.public_url}`
            : "ngrok is starting; this card updates when the public URL is ready."
        );
      } else if (kind === "stop") {
        setTunnelMessage("Tunnel stopped.");
      } else {
        setTunnelMessage(`ngrok ${next.version || ""} installed.`.trim());
      }
      return true;
    } catch (problem) {
      setTunnelMessage(problem instanceof Error ? problem.message : String(problem));
      return false;
    } finally {
      setTunnelBusy("");
    }
  }

  async function copyText(value: string, what: string) {
    try {
      await navigator.clipboard.writeText(value);
      setTunnelMessage(`${what} copied.`);
    } catch {
      setTunnelMessage("Copy failed; select the text and copy it manually.");
    }
  }

  async function toggleWorker(workerId: string, enabled: boolean) {
    setBusy(workerId);
    setError("");
    try {
      await api(`/api/workers/${workerId}/enabled`, {
        method: "POST",
        body: JSON.stringify({ enabled })
      });
      await load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy("");
    }
  }

  async function revokeWorker(workerId: string) {
    setBusy(workerId);
    setError("");
    try {
      await api(`/api/workers/${workerId}/revoke`, { method: "POST" });
      setConfirmRevoke("");
      await load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy("");
    }
  }

  const items = snapshot?.workers || [];

  return (
    <div className="page-stack">
      <header className="page-head">
        <div>
          <h1>Worker Nodes</h1>
          <p>
            Windows and Linux Mesh Clients report inventory and heartbeat. This server never
            installs or changes worker-side CLIs.
          </p>
        </div>
        <button className="btn ghost" onClick={() => void load()}>
          Refresh
        </button>
      </header>

      {error && <p className="error-box">{error}</p>}

      <Card
        title="Pair a Mesh Client"
        subtitle={
          snapshot
            ? `${snapshot.counts.online}/${snapshot.counts.workers} online · ${snapshot.pairing.active_codes.length} active code(s) · protocol v${snapshot.protocol_version}`
            : "Reading pairing state…"
        }
      >
        <div className="row">
          <input
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="Label for this worker (optional)"
          />
          <button className="btn primary" disabled={busy === "pairing"} onClick={() => void createPairing()}>
            {busy === "pairing" ? "Creating…" : "Create pairing code"}
          </button>
        </div>
        {pairing ? (
          <div className="pairing-box">
            <div>
              <span className="muted small">One-time code</span>
              <strong className="pairing-code">{pairing.code}</strong>
            </div>
            <div>
              <span className="muted small">Expires</span>
              <strong>{formatTime(pairing.expires_at)}</strong>
            </div>
            <div>
              <span className="muted small">Register endpoint</span>
              <code className="small">{pairing.worker_join_url}</code>
            </div>
            <p className="muted small">
              Give this code to the Mesh Client once. It is consumed on registration and cannot be
              reused; the worker token issued afterwards is what authenticates heartbeats.
            </p>
          </div>
        ) : (
          <p className="muted small">
            Pairing codes are created on demand and expire after{" "}
            {snapshot?.pairing.ttl_seconds ?? 900} seconds. Nothing is paired automatically.
          </p>
        )}
        {snapshot && snapshot.pairing.active_codes.length > 0 && (
          <div className="active-codes">
            {snapshot.pairing.active_codes.map((code) => (
              <span key={`${code.label}-${code.expires_at}`} className="chip chip-warn">
                {code.label || "unlabelled"} · expires {formatTime(code.expires_at)}
              </span>
            ))}
          </div>
        )}
      </Card>

      <TunnelCard
        tunnel={tunnel}
        pairing={pairing}
        label={label}
        busy={tunnelBusy}
        message={tunnelMessage}
        onAction={runTunnel}
        onCopy={(value, what) => void copyText(value, what)}
        onCreatePairing={() => void createPairing()}
      />

      {items.length === 0 ? (
        <Card title="No paired workers" subtitle="Real pairing data only">
          <Empty
            title="Nothing connected yet"
            detail="Pair a Mesh Client to see its real OS, architecture and detected agents here."
          />
        </Card>
      ) : (
        <div className="worker-page-grid">
          {items.map((worker) => (
            <Card
              key={worker.id}
              title={worker.name || worker.id}
              subtitle={`${worker.os_family} · ${worker.os_version || "unknown"} · ${worker.arch}`}
              className={worker.revoked ? "revoked" : ""}
            >
              <div className="detail-grid">
                <div>
                  <span>Status</span>
                  <StatusChip status={worker.revoked ? "revoked" : worker.status} />
                </div>
                <div>
                  <span>Heartbeat</span>
                  <strong>
                    {worker.last_heartbeat_at ? formatTime(worker.last_heartbeat_at) : "never"}
                    {worker.heartbeat_age_seconds !== null
                      ? ` · ${Math.round(worker.heartbeat_age_seconds)}s ago`
                      : ""}
                  </strong>
                </div>
                <div>
                  <span>Current task</span>
                  <strong>{worker.current_task_id || "—"}</strong>
                </div>
                <div>
                  <span>Detected agents</span>
                  <strong>{worker.agents.length}</strong>
                </div>
              </div>

              {worker.agents.length > 0 && (
                <div className="worker-agents">
                  {worker.agents.map((agent) => (
                    <div key={agent.kind} className="worker-agent-row">
                      <strong>{agent.kind}</strong>
                      <span className="mono small">{agent.version || "no version"}</span>
                      <StatusChip
                        status={agent.usable ? "available" : agent.installed ? "limited" : "not_detected"}
                      />
                    </div>
                  ))}
                </div>
              )}

              <pre className="json-box">{JSON.stringify(worker.inventory, null, 2)}</pre>

              {confirmRevoke === worker.id ? (
                <div className="confirm-box">
                  <strong>Revoke {worker.name || worker.id}?</strong>
                  <p>
                    Its socket is dropped and the worker token stops working. A revoked worker cannot
                    be re-enabled; issue a new pairing code instead.
                  </p>
                  <div className="row">
                    <button
                      className="btn danger"
                      disabled={busy === worker.id}
                      onClick={() => void revokeWorker(worker.id)}
                    >
                      Revoke permanently
                    </button>
                    <button className="btn ghost" onClick={() => setConfirmRevoke("")}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div className="detail-actions">
                  <button
                    className="btn ghost small-btn"
                    disabled={busy === worker.id || worker.revoked}
                    onClick={() => void toggleWorker(worker.id, !worker.enabled)}
                  >
                    {worker.enabled ? "Disable worker" : "Enable worker"}
                  </button>
                  <button
                    className="btn ghost small-btn"
                    disabled={busy === worker.id || worker.revoked}
                    onClick={() => setConfirmRevoke(worker.id)}
                  >
                    Revoke…
                  </button>
                </div>
              )}
            </Card>
          ))}
        </div>
      )}

      {snapshot && snapshot.counts.revoked > 0 && (
        <p className="muted small">
          {snapshot.counts.revoked} revoked worker record(s) are retained for audit. They cannot
          reconnect.
        </p>
      )}
    </div>
  );
}

export function BrowserPage({
  status,
  accounts,
  onRefresh
}: {
  status: ServerStatus;
  accounts: BrowserAccount[];
  onRefresh: () => void;
}) {
  return (
    <div className="page-stack">
      <header className="page-head">
        <div>
          <h1>Browser Automation</h1>
          <p>Existing ForgeMesh Browser runtime, consumed over HTTP. It is not rebuilt here.</p>
        </div>
        <button className="btn" onClick={onRefresh}>
          Refresh
        </button>
      </header>
      <Card title="ForgeMesh Browser" subtitle={status.browser.url}>
        <div className="detail-grid">
          <div>
            <span>Service</span>
            <StatusChip
              status={status.browser.connected ? "online" : "offline"}
              label={status.browser.connected ? "Connected" : "Unavailable"}
            />
          </div>
          <div>
            <span>Token file</span>
            <strong>{status.browser.token_present ? "present (0600)" : "missing"}</strong>
          </div>
        </div>
        {status.browser.error && <p className="error-box">{status.browser.error}</p>}
      </Card>
      <Card title="Provider accounts" subtitle="Used by the AI Council">
        {accounts.length === 0 ? (
          <Empty
            title="No accounts detected"
            detail="Sign in through the existing ForgeMesh Browser CLI, then refresh."
          />
        ) : (
          <div className="account-grid">
            {accounts.map((account, index) => (
              <div className="account-card" key={`${account.provider}-${index}`}>
                <strong>{account.provider || account.provider_id}</strong>
                <span className="mono small">{account.profile_id}</span>
                <StatusChip status={String(account.status || "unknown")} />
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

export function TerminalPage({ events }: { events: EventRecord[] }) {
  const [filter, setFilter] = useState("");
  const visible = events.filter(
    (event) =>
      !filter ||
      event.type.toLowerCase().includes(filter.toLowerCase()) ||
      JSON.stringify(event.data).toLowerCase().includes(filter.toLowerCase())
  );
  return (
    <div className="page-stack">
      <header className="page-head">
        <div>
          <h1>Terminal &amp; Logs</h1>
          <p>Durable events from SQLite. Reconnects replay missed events; nothing is generated for display.</p>
        </div>
        <input
          className="search"
          placeholder="Filter events…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
      </header>
      <Card title="Event stream" subtitle={`${visible.length} event(s)`}>
        <div className="terminal tall">
          {visible.map((event) => (
            <div key={event.id} className="terminal-line">
              <span className="terminal-id">#{event.id}</span>
              <span className="terminal-time">{formatTime(event.timestamp)}</span>
              <span className="terminal-type">{event.type}</span>
              <span className="terminal-data">{JSON.stringify(event.data)}</span>
            </div>
          ))}
          {visible.length === 0 && <div className="terminal-line muted">No matching events.</div>}
        </div>
      </Card>
    </div>
  );
}

export function DesktopCard() {
  const bridge = window.forgemeshDesktop;
  const [info, setInfo] = useState<DesktopInfo | null>(null);
  const [autostart, setAutostart] = useState<DesktopAutostart | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!bridge) return;
    let cancelled = false;
    void (async () => {
      try {
        const [nextInfo, nextAutostart] = await Promise.all([bridge.info(), bridge.autostart()]);
        if (cancelled) return;
        setInfo(nextInfo);
        setAutostart(nextAutostart);
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [bridge]);

  if (!bridge) return null;

  const modeLabel =
    info?.serverMode === "attached"
      ? "Attached to an already running server"
      : info?.serverMode === "running"
        ? "Owned by this desktop shell"
        : "Stopped";

  async function toggle(enabled: boolean) {
    if (!bridge) return;
    setBusy(true);
    setError("");
    try {
      const next = await bridge.setAutostart(enabled);
      setAutostart(next);
      if (next.error) setError(next.error);
      setInfo(await bridge.info());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function restart() {
    if (!bridge) return;
    setBusy(true);
    setError("");
    try {
      const result = await bridge.restart();
      if (!result.ok) setError(result.error);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Desktop Shell" subtitle="Electron window, tray and login behaviour">
      <div className="detail-grid">
        <div>
          <span>Shell version</span>
          <strong>{info ? `${info.version} · Electron ${info.electron}` : "reading…"}</strong>
        </div>
        <div>
          <span>Control server</span>
          <strong>{modeLabel}</strong>
        </div>
        <div>
          <span>Window state</span>
          <strong>{info ? (info.windowVisible ? "visible" : "hidden in tray") : "—"}</strong>
        </div>
        <div>
          <span>Package</span>
          <strong>{info ? (info.packaged ? "packaged build" : "development checkout") : "—"}</strong>
        </div>
      </div>

      <div className="row spread desktop-autostart">
        <label className="row">
          <input
            type="checkbox"
            checked={Boolean(autostart?.enabled)}
            disabled={busy}
            onChange={(event) => void toggle(event.target.checked)}
          />
          <span>Start at login</span>
        </label>
        <code className="small ellipsis">{autostart?.path || "—"}</code>
      </div>
      <p className="small muted desktop-note">
        Closing the window keeps the control server running in the tray. Starting at login writes a real
        XDG autostart entry, nothing else.
      </p>
      {error && <div className="error-box">{error}</div>}

      <div className="detail-actions">
        <button
          className="btn ghost small-btn"
          disabled={busy || info?.serverMode === "attached"}
          title={
            info?.serverMode === "attached"
              ? "This control server was started outside the desktop shell."
              : "Stop the owned control server and start it again."
          }
          onClick={() => void restart()}
        >
          Restart control server
        </button>
        <button className="btn ghost small-btn" onClick={() => void bridge.openLog()}>
          Open server log
        </button>
        <button className="btn ghost small-btn" onClick={() => void bridge.hide()}>
          Hide to tray
        </button>
      </div>
    </Card>
  );
}

export function SettingsPage({
  status,
  onOpenWizard,
  children
}: {
  status: ServerStatus;
  onOpenWizard: () => void;
  children: ReactNode;
}) {
  return (
    <div className="page-stack">
      <header className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Central server configuration. Secrets are never displayed.</p>
        </div>
        <button className="btn" onClick={onOpenWizard}>
          Open setup wizard
        </button>
      </header>
      <Card title="Runtime" subtitle="Read from the active server process">
        <div className="detail-grid">
          <div>
            <span>Service</span>
            <strong>{status.service} v{status.version}</strong>
          </div>
          <div>
            <span>Started</span>
            <strong>{status.started_at ? new Date(status.started_at).toLocaleString() : "—"}</strong>
          </div>
          <div>
            <span>Hostname</span>
            <strong>{String(status.host.hostname || "—")}</strong>
          </div>
          <div>
            <span>Platform</span>
            <strong>
              {String(status.host.distribution || status.host.os || "—")} · {String(status.host.arch || "")}
            </strong>
          </div>
          <div>
            <span>Setup state</span>
            <StatusChip status={status.setup.state} label={status.setup.state.replace("_", " ")} />
          </div>
          <div>
            <span>Auto-install on first run</span>
            <strong>{status.setup.auto_install ? "enabled" : "disabled"}</strong>
          </div>
        </div>
      </Card>
      <DesktopCard />
      {children}
    </div>
  );
}

export function TasksPage({
  tasks,
  projects,
  onRefresh
}: {
  tasks: TaskRecord[];
  projects: Project[];
  onRefresh: () => void;
}) {
  const [snapshot, setSnapshot] = useState<SchedulerSnapshot | null>(null);
  const [meetings, setMeetings] = useState<Array<{ id: string; agenda: string; status: string; mode: string }>>([]);
  const [projectId, setProjectId] = useState(projects[0]?.id || "");
  const [meetingId, setMeetingId] = useState("");
  const [maxTasks, setMaxTasks] = useState(12);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const load = async () => {
    const results = await Promise.allSettled([
      api<SchedulerSnapshot>("/api/scheduler/snapshot"),
      api<{ meetings: Array<{ id: string; agenda: string; status: string; mode: string }> }>(
        "/api/council/meetings?limit=50"
      )
    ]);
    if (results[0].status === "fulfilled") setSnapshot(results[0].value);
    if (results[1].status === "fulfilled") setMeetings(results[1].value.meetings);
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!projectId && projects[0]) setProjectId(projects[0].id);
  }, [projects, projectId]);

  async function act(path: string, body: Record<string, unknown> | null, label: string) {
    setBusy(label);
    setError("");
    try {
      await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      await load();
      onRefresh();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy("");
    }
  }

  const plannedMeetings = meetings.filter((meeting) => meeting.status === "completed");

  return (
    <div className="page-stack">
      <header className="page-head">
        <div>
          <h1>Task Queue</h1>
          <p>
            Planning, queueing and coding execution are separate from agent installation. Every run
            requires an approved gate and a real scheduler decision.
          </p>
        </div>
        <button className="btn ghost" onClick={() => void load()}>
          Refresh
        </button>
      </header>

      {error && <p className="error-box">{error}</p>}

      <Card
        title="Scheduler"
        subtitle={
          snapshot
            ? `${snapshot.active_runs}/${snapshot.max_concurrency} active · queue loop ${snapshot.queue_loop ? "running" : "stopped"}`
            : "Reading scheduler state…"
        }
      >
        <div className="scheduler-state">
          {snapshot ? (
            Object.keys(snapshot.by_status).length > 0 ? (
              Object.entries(snapshot.by_status).map(([status, count]) => (
                <div key={status} className="scheduler-count">
                  <span className="muted small">{status}</span>
                  <strong>{count}</strong>
                </div>
              ))
            ) : (
              <p className="muted small">No tasks have ever been created.</p>
            )
          ) : (
            <p className="muted small">Reading…</p>
          )}
        </div>
        <div className="detail-actions">
          <button
            className="btn primary"
            disabled={!!busy || snapshot?.queue_loop === true}
            onClick={() => void act("/api/scheduler/queue/start", null, "queue-start")}
          >
            {busy === "queue-start" ? "Starting…" : "Start queue loop"}
          </button>
          <button
            className="btn ghost"
            disabled={!!busy || snapshot?.queue_loop !== true}
            onClick={() => void act("/api/scheduler/queue/stop", null, "queue-stop")}
          >
            {busy === "queue-stop" ? "Stopping…" : "Stop queue loop"}
          </button>
        </div>
        <p className="muted small">
          The queue loop only starts tasks whose project approvals are already approved. It does not
          bypass the gate.
        </p>
      </Card>

      <Card title="Plan from a Council decision" subtitle="Creates tasks from stored proposals only">
        <div className="form-grid">
          <label className="field">
            <span>Project</span>
            <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
              <option value="">Select project…</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Completed meeting</span>
            <select value={meetingId} onChange={(event) => setMeetingId(event.target.value)}>
              <option value="">Select meeting…</option>
              {plannedMeetings.map((meeting) => (
                <option key={meeting.id} value={meeting.id}>
                  {meeting.agenda} ({meeting.id})
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Max tasks</span>
            <input
              type="number"
              min={1}
              max={20}
              value={maxTasks}
              onChange={(event) =>
                setMaxTasks(Math.max(1, Math.min(20, Number(event.target.value) || 1)))
              }
            />
          </label>
        </div>
        {plannedMeetings.length === 0 && (
          <p className="warn-box">
            No completed Council meeting exists yet. A plan cannot be inferred from a draft or a
            running meeting.
          </p>
        )}
        <button
          className="btn"
          disabled={!projectId || !meetingId || !!busy}
          onClick={() =>
            void act(
              "/api/scheduler/plan",
              { project_id: projectId, meeting_id: meetingId, max_tasks: maxTasks },
              "plan"
            )
          }
        >
          {busy === "plan" ? "Planning…" : "Plan tasks"}
        </button>
      </Card>

      <Card title="Tasks" subtitle={`${tasks.length} task(s)`}>
        {tasks.length === 0 ? (
          <Empty title="Queue is empty" detail="No task has been created yet." />
        ) : (
          <div className="task-list">
            {tasks.map((task) => (
              <div key={task.id} className="task-detail-row">
                <div className="task-detail-main">
                  <div className="row spread">
                    <strong>{task.title}</strong>
                    <StatusChip status={task.status} />
                  </div>
                  <small className="muted">
                    {task.id} · {task.worker_name || "unassigned"} ·{" "}
                    {task.agent_kind || "no agent"} · attempt {task.attempts ?? 0}
                  </small>
                  <div className="progress thin">
                    <i style={{ width: `${task.progress}%` }} />
                  </div>
                  {task.error && <p className="error-text small">{task.error}</p>}
                </div>
                <div className="task-detail-actions">
                  <button
                    className="btn ghost small-btn"
                    disabled={!!busy || ["running", "completed", "cancelled"].includes(task.status)}
                    onClick={() => void act(`/api/tasks/${task.id}/start`, null, task.id)}
                  >
                    Start
                  </button>
                  <button
                    className="btn ghost small-btn"
                    disabled={!!busy || !["queued", "running", "ready"].includes(task.status)}
                    onClick={() => void act(`/api/tasks/${task.id}/cancel`, null, `${task.id}-cancel`)}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
