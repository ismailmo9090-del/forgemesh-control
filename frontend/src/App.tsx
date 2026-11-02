import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, clearToken, saveToken, storedToken, websocketUrl } from "./api";
import type {
  BrowserAccount,
  CouncilSummary,
  DocumentRecord,
  EventRecord,
  Project,
  ServerStatus,
  TaskRecord
} from "./types";
import { SetupWizard } from "./components/SetupWizard";
import { CouncilPage } from "./CouncilPage";
import { AgentDetail, AgentsPage, BrowserPage, DocumentsPage, Overview, ProjectsPage, SettingsPage, TasksPage, TerminalPage, WorkersPage } from "./pages";
import { Modal, Sparkline } from "./components/ui";
import SettingsSections from "./SettingsSections";
import type { AiAccountSummary } from "./AiAccountsPage";

type Page =
  | "overview"
  | "projects"
  | "documents"
  | "council"
  | "agents"
  | "workers"
  | "browser"
  | "tasks"
  | "terminal"
  | "settings";

const NAV: Array<{ id: Page; label: string; glyph: string }> = [
  { id: "overview", label: "Overview", glyph: "⌂" },
  { id: "projects", label: "Projects", glyph: "▤" },
  { id: "documents", label: "Problem Statements", glyph: "◈" },
  { id: "council", label: "AI Council", glyph: "✳" },
  { id: "workers", label: "Worker Nodes", glyph: "▦" },
  { id: "agents", label: "AI Agents & Models", glyph: "❖" },
  { id: "tasks", label: "Task Queue", glyph: "☰" },
  { id: "browser", label: "Browser Automation", glyph: "◎" },
  { id: "terminal", label: "Terminal & Logs", glyph: "▸" },
  { id: "settings", label: "Settings", glyph: "⚙" }
];

function currentPage(): Page {
  const hash = window.location.hash.replace("#", "");
  return (NAV.find((item) => item.id === hash)?.id || "overview") as Page;
}

export default function App() {
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [page, setPage] = useState<Page>(currentPage());
  const [needToken, setNeedToken] = useState(false);
  const [fatal, setFatal] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [council, setCouncil] = useState<CouncilSummary | null>(null);
  const [accounts, setAccounts] = useState<BrowserAccount[]>([]);
  const [accountSummary, setAccountSummary] = useState<AiAccountSummary | null>(null);
  const [settingsSection, setSettingsSection] = useState<"coding-agents" | "ai-accounts">(
    "coding-agents"
  );
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [busy, setBusy] = useState<Record<string, string>>({});
  const [setupBusy, setSetupBusy] = useState<Record<string, string>>({});
  const [wizardDismissed, setWizardDismissed] = useState(false);
  const [wizardForced, setWizardForced] = useState(false);
  const [logs, setLogs] = useState<{ kind: string; text: string; path: string } | null>(null);
  const [managed, setManaged] = useState<string | null>(null);
  const [toast, setToast] = useState("");
  const autoStarted = useRef(false);
  const refreshTimer = useRef<number | null>(null);
  const refreshSeq = useRef(0);

  const notify = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 4200);
  }, []);

  const refresh = useCallback(async () => {
    const seq = ++refreshSeq.current;
    let snapshot: ServerStatus;
    try {
      snapshot = await api<ServerStatus>("/api/status");
    } catch (error) {
      if (seq !== refreshSeq.current) return;
      if (error instanceof ApiError && error.status === 401) setNeedToken(true);
      else setFatal(error instanceof Error ? error.message : String(error));
      return;
    }
    if (seq !== refreshSeq.current) return;
    setStatus(snapshot);
    setNeedToken(false);
    setFatal("");
    const results = await Promise.allSettled([
      api<{ projects: Project[] }>("/api/projects"),
      api<{ tasks: TaskRecord[] }>("/api/tasks?limit=100"),
      api<{ documents: DocumentRecord[] }>("/api/documents"),
      api<{ accounts: BrowserAccount[] }>("/api/browser/accounts"),
      api<{ events: EventRecord[] }>("/api/events/history?limit=200"),
      api<CouncilSummary>("/api/council/summary"),
      api<AiAccountSummary>("/api/accounts")
    ]);
    // A newer refresh may have started while these requests were in flight;
    // applying this stale batch would roll the dashboard back.
    if (seq !== refreshSeq.current) return;
    if (results[0].status === "fulfilled") setProjects(results[0].value.projects);
    if (results[1].status === "fulfilled") setTasks(results[1].value.tasks);
    if (results[2].status === "fulfilled") setDocuments(results[2].value.documents);
    if (results[3].status === "fulfilled") setAccounts(results[3].value.accounts);
    else setAccounts([]);
    if (results[4].status === "fulfilled") {
      const history = results[4].value.events;
      setEvents((previous) => mergeEvents(previous, history));
    }
    if (results[5].status === "fulfilled") setCouncil(results[5].value);
    if (results[6].status === "fulfilled") setAccountSummary(results[6].value);
  }, []);

  useEffect(() => {
    void refresh();
    refreshTimer.current = window.setInterval(() => void refresh(), 5000);
    return () => {
      if (refreshTimer.current) window.clearInterval(refreshTimer.current);
    };
  }, [refresh]);

  useEffect(() => {
    if (!status) return;
    if (status.setup.first_run_complete) return;
    if (
      status.setup.auto_install &&
      status.setup.state === "needs_setup" &&
      !status.setup.running &&
      !autoStarted.current
    ) {
      autoStarted.current = true;
      void startSetup();
    }
  }, [status?.setup.state, status?.setup.running, status?.setup.first_run_complete, status?.setup.auto_install]);

  useEffect(() => {
    if (needToken) return;
    let closed = false;
    let socket: WebSocket | null = null;
    let retry: number | null = null;
    let backoff = 2500;
    const connect = () => {
      if (closed) return;
      socket = new WebSocket(websocketUrl());
      socket.onopen = () => {
        backoff = 2500;
      };
      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(message.data) as {
            type: string;
            data: Record<string, unknown>;
          };
          if (payload.type === "snapshot") {
            setStatus(payload.data as unknown as ServerStatus);
          } else if (payload.type === "metrics") {
            const sample = payload.data as unknown as ServerStatus["metrics"];
            setStatus((previous) =>
              previous
                ? {
                    ...previous,
                    metrics: {
                      ...sample,
                      history: [...(previous.metrics.history || []).slice(-179), sample]
                    }
                  }
                : previous
            );
          } else if (payload.type === "event") {
            const event = payload.data as unknown as EventRecord;
            setEvents((previous) => mergeEvents(previous, [event]));
            if (
              event.type.startsWith("agent.") ||
              event.type.startsWith("setup.") ||
              event.type.startsWith("project.") ||
              event.type.startsWith("task.") ||
              event.type.startsWith("document.")
            ) {
              void refresh();
            }
          }
        } catch {
          /* ignore malformed frames; the server is the source of truth */
        }
      };
      socket.onclose = (event) => {
        if (closed) return;
        // 1008 (policy violation) and 4401 (token rejected) will not heal on
        // retry; ask for a fresh token instead of hammering the server.
        if (event.code === 1008 || event.code === 4401 || event.code === 4403) {
          setNeedToken(true);
          return;
        }
        backoff = Math.min(backoff * 2, 15000);
        retry = window.setTimeout(connect, backoff);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      closed = true;
      if (retry) window.clearTimeout(retry);
      socket?.close();
    };
  }, [needToken, refresh]);

  async function startSetup() {
    try {
      await api("/api/setup/start", { method: "POST" });
      setWizardForced(true);
      notify("Installing coding agents from their official sources…");
      void pollSetup();
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    }
  }

  async function pollSetup() {
    for (let attempt = 0; attempt < 900; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
      try {
        const snapshot = await api<ServerStatus>("/api/status");
        setStatus(snapshot);
        if (!snapshot.setup.running && snapshot.setup.complete) {
          await refresh();
          return;
        }
      } catch {
        return;
      }
    }
  }

  async function agentAction(
    kind: string,
    action: "install" | "verify" | "detect" | "reinstall",
    target: "setup" | "page" = "page"
  ) {
    const setter = target === "setup" ? setSetupBusy : setBusy;
    setter((previous) => ({ ...previous, [kind]: action }));
    try {
      const path =
        action === "install"
          ? `/api/agents/${kind}/install`
          : action === "reinstall"
            ? `/api/agents/${kind}/reinstall`
            : `/api/agents/${kind}/${action}`;
      const result = await api<Record<string, unknown>>(path, {
        method: "POST",
        body: action === "install" ? JSON.stringify({ force: false }) : undefined
      });
      const state = String(result.install_status || "done");
      notify(`${kind}: ${state}`);
      await refresh();
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
      await refresh();
    } finally {
      setter((previous) => {
        const next = { ...previous };
        delete next[kind];
        return next;
      });
    }
  }

  async function openLogs(kind: string) {
    try {
      const payload = await api<{ log: string; path: string }>(`/api/agents/${kind}/logs`);
      setLogs({ kind, text: payload.log || "(no log recorded yet)", path: payload.path });
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    }
  }

  async function detectAll() {
    try {
      await api("/api/agents/detect", { method: "POST" });
      notify("Detection finished for both central agents.");
      await refresh();
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    }
  }

  function navigate(next: Page) {
    setPage(next);
    window.location.hash = next;
  }

  function openSettings(section: "coding-agents" | "ai-accounts") {
    setSettingsSection(section);
    navigate("settings");
    setWizardForced(false);
    setWizardDismissed(true);
  }

  const managedAgent = managed
    ? status?.agents.find((agent) => agent.kind === managed) || null
    : null;

  if (needToken) {
    return (
      <TokenGate
        onSubmit={async (token) => {
          saveToken(token);
          try {
            const snapshot = await api<ServerStatus>("/api/status");
            setStatus(snapshot);
            setNeedToken(false);
            await refresh();
          } catch (error) {
            clearToken();
            if (error instanceof ApiError && error.status === 401) {
              throw new Error("That token was rejected. Paste the current control token and try again.");
            }
            throw error;
          }
        }}
      />
    );
  }

  if (!status) {
    return (
      <div className="boot-screen">
        <div className="mesh-mark large" aria-hidden>
          <i />
          <i />
          <i />
          <i />
        </div>
        <h1>FORGEMESH</h1>
        <p>{fatal || "Connecting to the central control server…"}</p>
        {fatal && (
          <button className="btn" onClick={() => void refresh()}>
            Retry
          </button>
        )}
      </div>
    );
  }

  const showWizard =
    (wizardForced || !status.setup.first_run_complete) && !wizardDismissed;
  const metrics = status.metrics;
  const history = metrics.history?.length ? metrics.history : [metrics];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="mesh-mark" aria-hidden>
            <i />
            <i />
            <i />
            <i />
          </div>
          <div>
            <strong>FORGEMESH</strong>
            <span>AI CONTROL CENTER</span>
          </div>
        </div>
        <nav>
          {NAV.map((item) => (
            <button
              key={item.id}
              className={page === item.id ? "active" : ""}
              onClick={() => navigate(item.id)}
            >
              <span className="nav-glyph">{item.glyph}</span>
              {item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="row">
            <div className="mesh-mark small" aria-hidden>
              <i />
              <i />
              <i />
              <i />
            </div>
            <div>
              <strong>ForgeMesh v{status.version}</strong>
              <span>AI Multi-Agent Control Center</span>
            </div>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="server-pill">
            <span className={`dot ${status.browser.connected ? "ok" : "warn"}`} />
            FORGEMESH SERVER
            <b>ONLINE</b>
          </div>
          <div className="top-metric server-name">
            <span>Central Server</span>
            <strong>{String(status.host.hostname || "localhost")}</strong>
            <small>{String(status.host.distribution || status.host.os || "")}</small>
          </div>
          <TopMetric
            label="CPU"
            value={`${metrics.cpu_percent.toFixed(0)}%`}
            values={history.map((item) => item.cpu_percent)}
          />
          <TopMetric
            label="RAM"
            value={`${metrics.ram_percent.toFixed(0)}%`}
            values={history.map((item) => item.ram_percent)}
          />
          <TopMetric
            label="Storage"
            value={`${metrics.disk_percent.toFixed(0)}%`}
            values={history.map((item) => item.disk_percent)}
          />
          <TopMetric
            label="Network"
            value={`↑${metrics.tx_kbps.toFixed(0)} ↓${metrics.rx_kbps.toFixed(0)}`}
            values={history.map((item) => item.rx_kbps + item.tx_kbps)}
            color="var(--info)"
            className="hide-md"
          />
          <div className="top-count hide-md">
            <span>Workers</span>
            <strong>{String(status.counts.workers_online).padStart(2, "0")}</strong>
          </div>
          <div className="top-count">
            <span>Tasks</span>
            <strong>{String(status.counts.tasks_running).padStart(2, "0")}</strong>
          </div>
          <div className="top-right">
            <span className="alert-badge" title="Pending human actions">
              {status.counts.human_actions_pending}
            </span>
            <div className="avatar">FM</div>
            <button
              className="link-btn"
              onClick={() => {
                clearToken();
                setNeedToken(true);
              }}
            >
              Lock
            </button>
          </div>
        </header>

        <main className="content">
          {page === "overview" && (
            <Overview
              status={status}
              projects={projects}
              tasks={tasks}
              accounts={accounts}
              events={events}
              council={council}
              busy={busy}
              onAction={(kind, action) => void agentAction(kind, action, "page")}
              onManage={(kind) => setManaged(kind)}
              onOpenLogs={(kind) => void openLogs(kind)}
              onOpenWizard={() => {
                setWizardDismissed(false);
                setWizardForced(true);
              }}
              onNavigate={(next) => navigate(next as Page)}
            />
          )}
          {page === "projects" && (
            <ProjectsPage projects={projects} onCreated={() => void refresh()} />
          )}
          {page === "documents" && (
            <DocumentsPage documents={documents} onUploaded={() => void refresh()} />
          )}
          {page === "council" && <CouncilPage projects={projects} />}
          {page === "agents" && (
            <AgentsPage
              status={status}
              busy={busy}
              onAction={(kind, action) => void agentAction(kind, action, "page")}
              onManage={(kind) => setManaged(kind)}
              onOpenLogs={(kind) => void openLogs(kind)}
              onDetectAll={() => void detectAll()}
            />
          )}
          {page === "workers" && <WorkersPage />}
          {page === "browser" && (
            <BrowserPage status={status} accounts={accounts} onRefresh={() => void refresh()} />
          )}
          {page === "tasks" && (
            <TasksPage tasks={tasks} projects={projects} onRefresh={() => void refresh()} />
          )}
          {page === "terminal" && <TerminalPage events={events} />}
          {page === "settings" && (
            <SettingsPage
              status={status}
              onOpenWizard={() => {
                setWizardDismissed(false);
                setWizardForced(true);
              }}
            >
              <SettingsSections section={settingsSection} onSection={setSettingsSection} />
            </SettingsPage>
          )}
        </main>
      </div>

      {showWizard && (
        <SetupWizard
          setup={status.setup}
          status={status}
          accounts={accountSummary}
          busy={setupBusy}
          canClose={status.setup.complete}
          onInstall={(kind) => void agentAction(kind, "install", "setup")}
          onVerify={(kind) => void agentAction(kind, "verify", "setup")}
          onDetect={(kind) => void agentAction(kind, "detect", "setup")}
          onLogs={(kind) => void openLogs(kind)}
          onOpenSettings={openSettings}
          onRefresh={() => void refresh()}
          onContinue={() => {
            setWizardForced(false);
            setWizardDismissed(true);
            void refresh();
          }}
        />
      )}

      {managedAgent && (
        <Modal title={`Agent management · ${managedAgent.display_name}`} onClose={() => setManaged(null)} width="920px">
          <AgentDetail
            agent={managedAgent}
            busy={busy[managedAgent.kind]}
            onAction={(action) => void agentAction(managedAgent.kind, action, "page")}
            onLogs={() => void openLogs(managedAgent.kind)}
            onClose={() => setManaged(null)}
          />
        </Modal>
      )}

      {logs && (
        <Modal title={`Installation logs · ${logs.kind}`} onClose={() => setLogs(null)} width="900px">
          <p className="muted small">{logs.path || "No log file path recorded."}</p>
          <pre className="log-box">{logs.text}</pre>
        </Modal>
      )}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function TopMetric({
  label,
  value,
  values,
  color,
  className = ""
}: {
  label: string;
  value: string;
  values: number[];
  color?: string;
  className?: string;
}) {
  return (
    <div className={`top-metric ${className}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <Sparkline values={values} color={color} height={30} fill={false} />
    </div>
  );
}

function TokenGate({ onSubmit }: { onSubmit: (token: string) => Promise<void> }) {
  const [value, setValue] = useState(storedToken());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <div className="boot-screen">
      <div className="mesh-mark large" aria-hidden>
        <i />
        <i />
        <i />
        <i />
      </div>
      <h1>FORGEMESH</h1>
      <p>The control server requires its local token (stored in the data directory, mode 0600).</p>
      <form
        className="token-form"
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          try {
            await onSubmit(value.trim());
          } catch (problem) {
            setError(problem instanceof Error ? problem.message : String(problem));
          } finally {
            setBusy(false);
          }
        }}
      >
        <input
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="Paste control token"
          spellCheck={false}
        />
        <button className="btn primary" disabled={!value.trim() || busy}>
          {busy ? "Checking…" : "Unlock"}
        </button>
      </form>
      {error && <p className="error-box">{error}</p>}
      <p className="muted small">
        Terminal: <code>cat ~/.local/share/forgemesh-control/runtime/control_token</code>
      </p>
    </div>
  );
}

function mergeEvents(previous: EventRecord[], incoming: EventRecord[]): EventRecord[] {
  const byId = new Map<number, EventRecord>();
  for (const event of [...previous, ...incoming]) byId.set(event.id, event);
  return [...byId.values()].sort((a, b) => a.id - b.id).slice(-500);
}
