import { useState } from "react";
import { api } from "../api";
import { useApp } from "../context";
import type { Agent } from "../types";
import {
  Button,
  Empty,
  Icon,
  Modal,
  Pill,
  SectionHeader,
  availabilityTone,
  formatDate,
  timeAgo
} from "../components/ui";

export default function Agents() {
  const { snapshot, refresh, busy, setBusy, toast, openWizard, openAuth } = useApp();
  const [selected, setSelected] = useState<Agent | null>(null);
  const [logout, setLogout] = useState<Agent | null>(null);
  const [provider, setProvider] = useState("");
  const [error, setError] = useState("");

  const run = async (name: string, action: () => Promise<unknown>, message: string, tone: "ok" | "warn" | "bad" = "ok") => {
    setBusy(name);
    setError("");
    try {
      await action();
      toast(message, tone);
      await refresh();
    } catch (err: unknown) {
      const text = (err as Error).message;
      setError(text);
      toast(text, "bad");
    } finally {
      setBusy("");
    }
  };

  const historyFor = (kind: string) => snapshot.auth_history.filter((row) => row.kind === kind);

  return (
    <div className="page">
      <SectionHeader
        title="Supported agents"
        subtitle="The roster is generated from the control server's OFFICIAL_SOURCES table; it is not user-editable."
        action={
          <Button
            size="sm"
            variant="secondary"
            icon="refresh"
            onClick={() => run("detect-all", () => Promise.all(snapshot.agents.map((agent) => api.detectAgent(agent.kind))), "Detection refreshed for both CLIs.")}
            disabled={busy === "detect-all"}
          >
            Detect all
          </Button>
        }
      />

      {error && (
        <div className="inline-alert bad">
          <Icon name="alert" size={17} />
          <span>{error}</span>
        </div>
      )}

      <div className="agent-table card flush">
        <div className="agent-table-head">
          <span>Agent</span>
          <span>Binary / path</span>
          <span>Availability</span>
          <span>Sign-in</span>
          <span>Last check</span>
          <span />
        </div>
        {snapshot.agents.map((agent) => (
          <div key={agent.kind} className="agent-table-row">
            <div className="agent-cell-name">
              <div className={`agent-glyph ${agent.kind}`}>{agent.kind === "opencode" ? "OC" : "Q"}</div>
              <div>
                <strong>{agent.display_name}</strong>
                <span className="mono small">{agent.kind}</span>
              </div>
            </div>
            <div className="agent-cell-path">
              <span className="mono">{agent.binary}</span>
              <span className="small muted ellipsis">{agent.path || "not detected"}</span>
              {agent.version && <span className="small">{agent.version}</span>}
            </div>
            <div>
              <Pill tone={availabilityTone(agent.availability)}>{agent.availability}</Pill>
              {agent.usable_verified_at && (
                <span className="small muted block">verified {timeAgo(agent.usable_verified_at)}</span>
              )}
            </div>
            <div>
              <span className={`auth-state ${agent.auth_status}`}>{agent.auth_status || "unknown"}</span>
              <span className="small muted block ellipsis">{agent.auth_detail || "—"}</span>
            </div>
            <div>
              <span className="small">{agent.checked_at ? timeAgo(agent.checked_at) : "—"}</span>
              {agent.error && <span className="small bad block ellipsis">{agent.error}</span>}
            </div>
            <div className="agent-cell-actions">
              {!agent.installed ? (
                <Button size="sm" icon="download" onClick={() => openWizard({ kind: agent.kind, operation: "install" })}>
                  Install
                </Button>
              ) : (
                <>
                  <Button size="sm" variant="secondary" icon="check" onClick={() => openWizard({ kind: agent.kind, operation: "verify" })}>
                    Verify
                  </Button>
                  <Button size="sm" variant="secondary" icon="terminal" onClick={() => openAuth({ kind: agent.kind })}>
                    Sign in
                  </Button>
                </>
              )}
              <button className="icon-btn" onClick={() => setSelected(agent)} title="Details" aria-label="Details">
                <Icon name="info" size={17} />
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <div>
              <h2>Install discipline</h2>
              <p>What the installer actually does, in order.</p>
            </div>
          </div>
          <ol className="stage-list numbered">
            <li><span>1</span><div><strong>Detect</strong><p>Known paths plus PATH; record the real <span className="mono">--version</span> output.</p></div></li>
            <li><span>2</span><div><strong>Official HTTPS script</strong><p>Downloaded to a private temp file, never piped from <span className="mono">curl</span>.</p></div></li>
            <li><span>3</span><div><strong>No shell rc edits</strong><p>The app owns PATH for its child processes; it does not rewrite your shell profile.</p></div></li>
            <li><span>4</span><div><strong>npm fallback</strong><p>Only when the documented script route fails; both routes are recorded in the job log.</p></div></li>
            <li><span>5</span><div><strong>Functional verify</strong><p>A real prompt must return <span className="mono">FMCM_VERIFY_OK</span> before “usable” is shown.</p></div></li>
          </ol>
        </section>

        <section className="card">
          <div className="card-head">
            <div>
              <h2>Sign-in history</h2>
              <p>Every attempt is kept locally, with its CLI-derived result.</p>
            </div>
          </div>
          {snapshot.auth_history.length === 0 ? (
            <Empty title="No sign-in attempts" detail="Start a sign-in from the roster or a row below to record a real PTY session." />
          ) : (
            <div className="history-list">
              {snapshot.auth_history.slice(0, 10).map((row) => (
                <button key={row.id} className="history-row" onClick={() => openAuth({ kind: row.kind, sessionId: row.id })}>
                  <Pill tone={row.outcome === "authenticated" ? "green" : row.outcome === "cancelled" ? "amber" : "neutral"}>
                    {row.outcome || row.status}
                  </Pill>
                  <strong>{row.kind}</strong>
                  <span className="mono small">{row.mode}</span>
                  <span className="muted small">{timeAgo(row.started_at)}</span>
                  <span className="ellipsis small">{row.auth_detail || row.command}</span>
                </button>
              ))}
            </div>
          )}
        </section>
      </div>

      {selected && (
        <Modal
          wide
          title={selected.display_name}
          subtitle={`${selected.kind} · ${selected.binary}`}
          onClose={() => setSelected(null)}
          footer={
            <div className="row spread">
              <div className="row">
                <Button size="sm" variant="secondary" icon="refresh" onClick={() => run(`detect-${selected.kind}`, () => api.detectAgent(selected.kind), `${selected.display_name} detection refreshed.`)}>
                  Detect
                </Button>
                {selected.installed && (
                  <Button size="sm" variant="secondary" icon="download" onClick={() => { setSelected(null); openWizard({ kind: selected.kind, operation: "update" }); }}>
                    Update
                  </Button>
                )}
                {selected.installed && (
                  <Button size="sm" variant="secondary" icon="check" onClick={() => { setSelected(null); openWizard({ kind: selected.kind, operation: "verify" }); }}>
                    Verify usable
                  </Button>
                )}
              </div>
              <Button variant="secondary" onClick={() => setSelected(null)}>Close</Button>
            </div>
          }
        >
          <div className="detail-hero">
            <div className={`agent-glyph large ${selected.kind}`}>{selected.kind === "opencode" ? "OC" : "Q"}</div>
            <div>
              <div className="row">
                <Pill tone={availabilityTone(selected.availability)}>{selected.availability}</Pill>
                <span className={`auth-state ${selected.auth_status}`}>{selected.auth_status || "unknown"}</span>
              </div>
              <p>{selected.auth_detail || selected.error || "No extra detail from the last real check."}</p>
            </div>
          </div>
          <dl className="kv">
            <div><dt>Binary</dt><dd className="mono">{selected.binary}</dd></div>
            <div><dt>Path</dt><dd className="mono ellipsis">{selected.path || "—"}</dd></div>
            <div><dt>Version</dt><dd className="mono">{selected.version || "—"}</dd></div>
            <div><dt>Platform</dt><dd>{selected.platform?.os} {selected.platform?.arch} · {selected.platform?.supported ? "supported" : selected.platform?.detail || "unverified"}</dd></div>
            <div><dt>Verified usable</dt><dd>{selected.usable_verified_at ? formatDate(selected.usable_verified_at) : "never"}</dd></div>
            <div><dt>Official script</dt><dd className="mono ellipsis">{selected.script_url}</dd></div>
            <div><dt>npm package</dt><dd className="mono">{selected.npm_package || "—"}</dd></div>
            <div><dt>Docs</dt><dd className="mono ellipsis">{selected.docs_url}</dd></div>
          </dl>
          <h4 className="small-head">Detection paths</h4>
          <div className="path-list">
            {selected.known_paths.map((path) => (
              <span key={path} className={`mono ${path === selected.path ? "present" : ""}`}>
                {path === selected.path ? "● " : "○ "}{path}
              </span>
            ))}
          </div>

          {selected.installed && (
            <div className="agent-detail-actions">
              <Button size="sm" variant="secondary" icon="terminal" onClick={() => { setSelected(null); openAuth({ kind: selected.kind }); }}>
                Host sign-in
              </Button>
              <Button size="sm" variant="secondary" icon="external" onClick={() => run(`terminal-${selected.kind}`, () => api.openTerminal(selected.kind), "External terminal launch requested; the daemon reports whether it found an emulator.")}>
                Open external terminal
              </Button>
              <Button size="sm" variant="danger" icon="power" onClick={() => { setLogout(selected); setProvider(""); }}>
                Log out
              </Button>
            </div>
          )}

          <h4 className="small-head">Recent attempts</h4>
          {historyFor(selected.kind).length === 0 ? (
            <p className="small muted">No recorded attempts for this agent.</p>
          ) : (
            <div className="history-list tight">
              {historyFor(selected.kind).slice(0, 5).map((row) => (
                <button key={row.id} className="history-row" onClick={() => { setSelected(null); openAuth({ kind: row.kind, sessionId: row.id }); }}>
                  <Pill tone={row.outcome === "authenticated" ? "green" : row.outcome === "cancelled" ? "amber" : "neutral"}>{row.outcome}</Pill>
                  <span className="small">{timeAgo(row.started_at)}</span>
                  <span className="small ellipsis">{row.auth_detail || row.command}</span>
                </button>
              ))}
            </div>
          )}
        </Modal>
      )}

      {logout && (
        <Modal
          title={`Sign out of ${logout.display_name}`}
          subtitle="This asks the CLI's own logout command; status is re-read afterwards."
          onClose={() => setLogout(null)}
          footer={
            <div className="row spread">
              <span className="small muted">POST /api/agents/{logout.kind}/logout</span>
              <Button
                variant="danger"
                icon="power"
                onClick={async () => {
                  await run(`logout-${logout.kind}`, () => api.logout(logout.kind, provider), `${logout.display_name} logout requested.`, "warn");
                  setLogout(null);
                }}
              >
                Log out
              </Button>
            </div>
          }
        >
          {logout.kind === "opencode" ? (
            <div className="field">
              <label>Provider to log out</label>
              <input value={provider} onChange={(event) => setProvider(event.target.value)} placeholder="for example anthropic" />
              <small>OpenCode logs out one provider at a time; the agent adapter refuses an empty provider.</small>
            </div>
          ) : (
            <p>{logout.display_name} exposes a single documented logout command. It runs as the current user.</p>
          )}
        </Modal>
      )}
    </div>
  );
}
