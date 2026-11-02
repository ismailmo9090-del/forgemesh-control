import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, shell } from "./api";
import { AppContext, type AppContextValue, type AuthRequest, type PageId, type WizardRequest } from "./context";
import type { Snapshot } from "./types";
import {
  Button,
  Icon,
  Logo,
  Pill,
  connectionLabel,
  connectionTone,
  timeAgo
} from "./components/ui";
import { PairingForm } from "./components/PairingForm";
import { AgentWizard } from "./components/AgentWizard";
import { AuthTerminal } from "./components/AuthTerminal";
import Dashboard from "./pages/Dashboard";
import Agents from "./pages/Agents";
import Tasks from "./pages/Tasks";
import Review from "./pages/Review";
import Approvals from "./pages/Approvals";
import Activity from "./pages/Activity";
import Settings from "./pages/Settings";
import About from "./pages/About";

const NAV: Array<{ id: PageId; label: string; icon: Parameters<typeof Icon>[0]["name"]; hint: string }> = [
  { id: "dashboard", label: "Dashboard", icon: "dashboard", hint: "Connection and supervision" },
  { id: "agents", label: "Agents", icon: "agent", hint: "OpenCode and Qoder" },
  { id: "tasks", label: "Tasks", icon: "tasks", hint: "Live local work" },
  { id: "review", label: "Review", icon: "build", hint: "Output laptop review" },
  { id: "approvals", label: "Approvals", icon: "gate", hint: "Human gates" },
  { id: "activity", label: "Activity", icon: "activity", hint: "Events and audit" },
  { id: "settings", label: "Settings", icon: "settings", hint: "Pairing and security" },
  { id: "about", label: "About", icon: "about", hint: "Versions and paths" }
];

const TITLES: Record<PageId, { title: string; subtitle: string }> = {
  dashboard: { title: "Worker overview", subtitle: "Supervision, connection health and live work on this laptop." },
  agents: { title: "Local agents", subtitle: "Install, verify and sign in to the two supported CLIs." },
  tasks: { title: "Tasks", subtitle: "Every task this worker has seen, from the server and this process." },
  review: { title: "Review workspace", subtitle: "Inspect the project workspace, artifacts and delivery evidence." },
  approvals: { title: "Approval gates", subtitle: "Decide blueprint, UI and implementation stages." },
  activity: { title: "Activity", subtitle: "Local bridge events plus the server audit history." },
  settings: { title: "Settings", subtitle: "Pairing, transport security, worker ownership and storage." },
  about: { title: "About", subtitle: "Runtime versions, paths and protocol identity." }
};

interface ToastState {
  id: number;
  message: string;
  tone: "ok" | "warn" | "bad";
}

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [bridgeError, setBridgeError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [page, setPage] = useState<PageId>("dashboard");
  const [busy, setBusy] = useState("");
  const [toastState, setToastState] = useState<ToastState | null>(null);
  const [wizard, setWizard] = useState<WizardRequest | null>(null);
  const [auth, setAuth] = useState<AuthRequest | null>(null);
  const [pairingOpen, setPairingOpen] = useState(false);
  const [daemonDown, setDaemonDown] = useState(false);
  const [cpuHistory, setCpuHistory] = useState<number[]>([]);
  const [memHistory, setMemHistory] = useState<number[]>([]);
  const refreshLock = useRef(false);
  const pendingRefresh = useRef<number | null>(null);
  const toastTimer = useRef<number | null>(null);

  const toast = useCallback((message: string, tone: "ok" | "warn" | "bad" = "ok") => {
    setToastState({ id: Date.now(), message, tone });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToastState(null), 5200);
  }, []);

  const refresh = useCallback(async () => {
    if (refreshLock.current) {
      if (pendingRefresh.current) window.clearTimeout(pendingRefresh.current);
      pendingRefresh.current = window.setTimeout(() => void refresh(), 250);
      return;
    }
    refreshLock.current = true;
    try {
      const next = await api.state();
      setSnapshot(next);
      setDaemonDown(false);
      setLoadError("");
      if (next.system?.cpu_percent != null) {
        setCpuHistory((values) => [...values.slice(-39), Number(next.system.cpu_percent)]);
      }
      if (next.system?.mem_percent != null) {
        setMemHistory((values) => [...values.slice(-39), Number(next.system.mem_percent)]);
      }
    } catch (error) {
      if (error instanceof ApiError && error.code === "no_bridge") {
        setBridgeError(error.message);
      } else {
        setDaemonDown(true);
        setLoadError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      refreshLock.current = false;
    }
  }, []);

  useEffect(() => {
    if (!window.forgemeshClient) {
      setBridgeError(
        "The Electron preload bridge is unavailable. Launch the desktop app with `npm start` in client/desktop; a plain browser tab cannot reach the local daemon."
      );
      return;
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    const offEvent = shell.onEvent(() => void refresh());
    const offState = shell.onDaemonState((state) => {
      setDaemonDown(state === "down");
      if (state === "up") void refresh();
    });
    return () => {
      window.clearInterval(timer);
      offEvent();
      offState();
    };
  }, [refresh]);

  useEffect(() => {
    if (snapshot && !snapshot.connection.paired && !pairingOpen) setPairingOpen(true);
  }, [snapshot?.connection.paired]); // eslint-disable-line react-hooks/exhaustive-deps

  const navigate = useCallback((next: PageId) => {
    setPage(next);
    window.scrollTo({ top: 0 });
    document.querySelector(".content")?.scrollTo({ top: 0 });
  }, []);

  const context = useMemo<AppContextValue | null>(() => {
    if (!snapshot) return null;
    return {
      snapshot,
      refresh,
      busy,
      setBusy,
      toast,
      navigate,
      openWizard: setWizard,
      openAuth: setAuth,
      openPairing: () => setPairingOpen(true)
    };
  }, [snapshot, refresh, busy, toast, navigate]);

  if (bridgeError) {
    return (
      <div className="boot-screen">
        <Logo />
        <h1>Desktop bridge not detected</h1>
        <p>{bridgeError}</p>
        <pre className="code-block">cd client/desktop{"\n"}npm start</pre>
      </div>
    );
  }

  if (!snapshot || !context) {
    return (
      <div className="boot-screen">
        <Logo />
        <div className="boot-progress" aria-hidden>
          <i />
        </div>
        <h1>Starting the local bridge</h1>
        <p>{loadError || "Reading real state from the loopback daemon…"}</p>
        {daemonDown && <Button icon="refresh" onClick={() => void refresh()}>Retry</Button>}
      </div>
    );
  }

  const connection = snapshot.connection;
  const worker = snapshot.worker;
  const unpaired = !connection.paired;
  const current = TITLES[page];

  return (
    <AppContext.Provider value={context}>
      <div className="shell">
        <aside className="sidebar">
          <Logo />
          <button
            className={`pair-card ${connection.paired ? "" : "attention"}`}
            onClick={() => setPairingOpen(true)}
            title="Pairing details"
          >
            <span className={`dot ${connection.paired ? "ok" : "warn"}`} />
            <span>
              <strong>{connection.paired ? connection.worker_name || "Paired device" : "Not paired"}</strong>
              <small>
                {connection.paired
                  ? `${connection.worker_id} · ${connection.token_fingerprint}`
                  : "Add a server pairing code"}
              </small>
            </span>
          </button>
          <nav>
            {NAV.map((item) => (
              <button
                key={item.id}
                className={page === item.id ? "active" : ""}
                onClick={() => navigate(item.id)}
                title={item.hint}
              >
                <Icon name={item.icon} size={18} />
                <span>{item.label}</span>
                {item.id === "approvals" && pendingApprovals(snapshot) > 0 && (
                  <b className="nav-badge">{pendingApprovals(snapshot)}</b>
                )}
                {item.id === "tasks" && snapshot.tasks.some((task) => task.status === "running") && (
                  <i className="nav-live" title="Task running" />
                )}
              </button>
            ))}
          </nav>
          <div className="sidebar-foot">
            <span>v{snapshot.app.version}</span>
            <span>protocol {snapshot.app.protocol_version}</span>
          </div>
        </aside>

        <div className="main">
          <header className="topbar">
            <div className="topbar-title">
              <h1>{current.title}</h1>
              <p>{current.subtitle}</p>
            </div>
            <div className="topbar-actions">
              <Pill tone={connectionTone(connection.state)}>{connectionLabel(connection.state)}</Pill>
              <span className="topbar-meta" title="Last worker heartbeat">
                <Icon name="activity" size={15} />
                {timeAgo(connection.last_heartbeat_at)}
              </span>
              <Button size="sm" variant="secondary" icon="refresh" onClick={() => void refresh()}>
                Refresh
              </Button>
              {connection.paired && (
                worker.pid ? (
                  <Button
                    size="sm"
                    variant="danger"
                    icon="stop"
                    disabled={busy === "worker-stop"}
                    onClick={async () => {
                      setBusy("worker-stop");
                      try {
                        await api.stopWorker(false);
                        toast("Worker stopped.", "warn");
                        await refresh();
                      } catch (error) {
                        toast(errorMessage(error), "bad");
                      } finally {
                        setBusy("");
                      }
                    }}
                  >
                    Stop
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant="success"
                    icon="play"
                    disabled={busy === "worker-start"}
                    onClick={async () => {
                      setBusy("worker-start");
                      try {
                        await api.startWorker(false);
                        toast("Worker started.", "ok");
                        await refresh();
                      } catch (error) {
                        toast(errorMessage(error), "bad");
                      } finally {
                        setBusy("");
                      }
                    }}
                  >
                    Start worker
                  </Button>
                )
              )}
            </div>
          </header>

          {daemonDown && (
            <div className="inline-alert bad">
              <Icon name="alert" size={17} />
              <span>Local bridge is unreachable: {loadError || "connection closed"}. The window keeps retrying.</span>
            </div>
          )}
          {connection.insecure_http && (
            <div className="inline-alert warn">
              <Icon name="shield" size={17} />
              <span>{connection.insecure_warning}</span>
            </div>
          )}
          {connection.token_warning && (
            <div className="inline-alert warn">
              <Icon name="info" size={17} />
              <span>{connection.token_warning}</span>
            </div>
          )}

          <main className="content">
            {unpaired && page === "dashboard" ? (
              <div className="onboarding">
                <div className="onboarding-copy">
                  <span className="eyebrow">First run</span>
                  <h2>Pair this laptop with your ForgeMesh control server.</h2>
                  <p>
                    The Client Manager supervises the real Python worker on this machine. Nothing is
                    registered, installed or reported until the server accepts a pairing code that a
                    human created on the dashboard.
                  </p>
                  <ul className="checklist">
                    <li><Icon name="check" size={15} /> Pairing codes are single-use and expire after 10 minutes.</li>
                    <li><Icon name="check" size={15} /> The worker token is stored in Secret Service when available, otherwise a 0600 file.</li>
                    <li><Icon name="check" size={15} /> Plain HTTP to a non-loopback server needs a typed confirmation.</li>
                  </ul>
                </div>
                <PairingForm onDone={() => { setPairingOpen(false); void refresh(); }} />
              </div>
            ) : page === "dashboard" ? (
              <Dashboard cpuHistory={cpuHistory} memHistory={memHistory} />
            ) : page === "agents" ? (
              <Agents />
            ) : page === "tasks" ? (
              <Tasks />
            ) : page === "review" ? (
              <Review />
            ) : page === "approvals" ? (
              <Approvals />
            ) : page === "activity" ? (
              <Activity />
            ) : page === "settings" ? (
              <Settings />
            ) : (
              <About />
            )}
          </main>
        </div>
      </div>

      {pairingOpen && (
        <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setPairingOpen(false)}>
          <div className="modal" role="dialog" aria-modal="true" aria-label="Pairing">
            <header>
              <div>
                <h2>{connection.paired ? "Connection pairing" : "Pair this device"}</h2>
                <p>
                  {connection.paired
                    ? "Current identity and how to rotate or remove it."
                    : "Paste the single-use code from the server dashboard."}
                </p>
              </div>
              <button className="icon-btn" onClick={() => setPairingOpen(false)} aria-label="Close">
                <Icon name="close" size={18} />
              </button>
            </header>
            <div className="modal-body">
              <PairingForm
                compact
                onDone={() => {
                  setPairingOpen(false);
                  void refresh();
                }}
              />
            </div>
          </div>
        </div>
      )}

      {wizard && (
        <AgentWizard
          kind={wizard.kind}
          operation={wizard.operation}
          onClose={() => setWizard(null)}
          onDone={() => void refresh()}
        />
      )}

      {auth && (
        <AuthTerminal
          kind={auth.kind}
          sessionId={auth.sessionId}
          onClose={() => setAuth(null)}
          onDone={() => void refresh()}
        />
      )}

      {toastState && (
        <div key={toastState.id} className={`toast ${toastState.tone}`} role="status">
          <Icon name={toastState.tone === "bad" ? "alert" : toastState.tone === "warn" ? "info" : "check"} size={17} />
          <span>{toastState.message}</span>
        </div>
      )}
    </AppContext.Provider>
  );
}

function pendingApprovals(snapshot: Snapshot): number {
  return snapshot.operator.projects.reduce((total, project) => {
    const approval = (project as unknown as { approval_states?: Record<string, { state: string }> }).approval_states;
    if (!approval) return total;
    return total + Object.values(approval).filter((stage) => stage.state === "pending").length;
  }, 0);
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "server_unreachable") return error.message;
    return error.message;
  }
  return error instanceof Error ? error.message : String(error);
}
