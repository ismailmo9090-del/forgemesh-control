import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, formatTime } from "./api";
import { AvailabilityChip, Card, Empty, StatusChip } from "./components/ui";
import { HostedTerminal } from "./HostedTerminal";
import type {
  AgentAuthState,
  AgentCliAccount,
  AgentInfo,
  AgentLogText,
  AgentModelList
} from "./types";

/**
 * Coding agents are managed through what their own CLI advertises. Every card
 * shows the capability record the server read from `--help`, and any operation
 * the CLI does not support is refused by the server and reported here verbatim
 * instead of being simulated.
 */

const ACTIONS: Array<{ label: string; action: string; verb: string; confirm?: boolean }> = [
  { label: "Detect", action: "detect", verb: "Re-reading the CLI from disk" },
  { label: "Verify installation", action: "verify", verb: "Running the CLI's own status checks" },
  { label: "Install", action: "install", verb: "Installing from the official source" },
  { label: "Update", action: "update", verb: "Updating through the CLI's official updater", confirm: true },
  { label: "Reinstall", action: "reinstall", verb: "Removing and installing again", confirm: true }
];

export default function CodingAgentsPage() {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    try {
      const next = await api<{ agents: AgentInfo[] }>("/api/agents");
      setAgents(next.agents);
      setLoadError("");
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : String(caughtMessage(error)));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(timer);
  }, [load]);

  return (
    <div className="page-stack">
      <Card
        title="Coding agents"
        subtitle="Installed CLIs, their authentication state and the accounts ForgeMesh can isolate for them."
        action={
          <button className="btn small-btn" onClick={() => void load()}>
            Refresh
          </button>
        }
      >
        {loadError && <p className="error-box">{loadError}</p>}
        {agents.length === 0 && !loadError ? (
          <Empty
            title="No coding agent registered"
            detail="The registry has nothing to report yet. Detection runs on the server when it starts."
          />
        ) : (
          <p className="desktop-note">
            Install here affects the central server only. Worker laptops are paired and provisioned
            by their own team and are never touched from this dashboard. Sign-in always runs the
            CLI's own documented login command; ForgeMesh stores no credential of its own.
          </p>
        )}
      </Card>
      {agents.map((agent) => (
        <AgentConsole key={agent.kind} agent={agent} onChanged={() => void load()} />
      ))}
    </div>
  );
}

function caughtMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function AgentConsole({ agent, onChanged }: { agent: AgentInfo; onChanged: () => void }) {
  const [auth, setAuth] = useState<AgentAuthState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [confirming, setConfirming] = useState("");
  const [terminal, setTerminal] = useState<{ account: AgentCliAccount | null; provider: string } | null>(
    null
  );
  const [logs, setLogs] = useState<AgentLogText | null>(null);
  const [models, setModels] = useState<AgentModelList | null>(null);
  const [newAccount, setNewAccount] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const reloadAuth = useCallback(async () => {
    try {
      const next = await api<AgentAuthState>(`/api/agents/${agent.kind}/auth`);
      if (mounted.current) {
        setAuth(next);
        setError("");
      }
    } catch (caught) {
      if (mounted.current) setError(caughtMessage(caught));
    }
  }, [agent.kind]);

  useEffect(() => {
    void reloadAuth();
  }, [reloadAuth, agent.auth_status, agent.availability]);

  const run = async (
    action: string,
    request: () => Promise<unknown>,
    onDone?: (value: any) => void
  ): Promise<boolean> => {
    setBusy(action);
    setNotice("");
    setError("");
    let ok = false;
    try {
      const value = await request();
      onDone?.(value);
      ok = true;
    } catch (caught) {
      const message = caughtMessage(caught);
      setNotice(message);
      setError(message);
    } finally {
      setBusy("");
      setConfirming("");
      await reloadAuth();
      onChanged();
    }
    return ok;
  };

  const capability = auth?.capability;
  const meta = (agent.metadata || {}) as Record<string, any>;
  const modelCount = models ? models.models.length : Number(meta.model_count || 0);
  const functional = meta.functional_check || {};

  const accountLabel = useMemo(() => {
    if (auth?.active_account) return auth.active_account;
    const active = (auth?.accounts || []).find((item) => item.active);
    return active ? active.name : "none";
  }, [auth]);

  // Some CLIs run real prompts with no credential at all. Telling the user to
  // sign in before they can use one of those would be a false blocker.
  const signInOptional =
    (capability ? capability.required_for_basic_use : meta.auth_required_for_use) === false;

  return (
    <Card
      title={agent.display_name}
      subtitle={
        capability?.limitation
          ? `CLI limitation: ${capability.limitation}`
          : "Capability record has not been read yet - run Detect."
      }
      live={agent.availability === "available"}
    >
      <div className="detail-grid">
        <div>
          <span>Install</span>
          <strong>
            <StatusChip status={agent.install_status} /> {agent.installed ? "present" : "absent"}
          </strong>
        </div>
        <div>
          <span>Version</span>
          <strong className="mono">{agent.version || "—"}</strong>
        </div>
        <div>
          <span>Path</span>
          <strong className="mono ellipsis" title={agent.path}>
            {agent.path || "—"}
          </strong>
        </div>
        <div>
          <span>Auth status</span>
          <strong>
            <StatusChip
              status={agent.auth_status}
              tone={
                agent.auth_status === "unauthenticated" && signInOptional ? "info" : undefined
              }
              label={
                agent.auth_status === "authenticated"
                  ? "signed in"
                  : agent.auth_status === "unauthenticated"
                    ? signInOptional
                      ? "sign-in optional"
                      : "not signed in"
                    : agent.auth_status
              }
            />
          </strong>
          <small>
            {agent.auth_status === "unauthenticated" && signInOptional
              ? "runs without a credential - signing in is your choice"
              : `read ${formatTime(agent.auth_checked_at)}`}
          </small>
        </div>
        <div>
          <span>Connected account</span>
          <strong>{accountLabel}</strong>
          <small>
            {auth ? `${auth.signed_in} of ${auth.accounts.length} account(s) signed in` : "reading…"}
          </small>
        </div>
        <div>
          <span>Model availability</span>
          <strong>{modelCount > 0 ? `${modelCount} model(s)` : "none read yet"}</strong>
          <small>
            {functional.verified
              ? `functional check ok${functional.model ? ` · ${functional.model}` : ""}`
              : functional.auth_required
                ? "blocked until the CLI is signed in"
                : functional.detail || meta.auth_detail || "not verified"}
          </small>
        </div>
        <div>
          <span>Last verification</span>
          <strong>{formatTime(agent.verified_at)}</strong>
          <small>detected {formatTime(agent.detected_at)}</small>
        </div>
        <div>
          <span>Availability</span>
          <AvailabilityChip status={agent.availability} functional={functional} />
          <small>
            {agent.enabled ? "enabled" : "disabled by operator"} ·{" "}
            {agent.os_supported ? `${String(meta.platform?.detail || "platform ok")}` : "unsupported platform"}
          </small>
        </div>
      </div>

      {agent.last_error && <p className="error-box">{agent.last_error}</p>}
      {error && <p className="error-box">{error}</p>}
      {meta.auth_detail && !meta.functional_check?.verified && (
        <p className="warn-box">{meta.auth_detail}</p>
      )}

      <div className="agent-actions">
        {ACTIONS.map((item) => {
          const needsConfirm = Boolean(item.confirm);
          const isConfirming = confirming === item.action;
          return (
            <button
              key={item.action}
              className={`btn small-btn${isConfirming ? " primary" : ""}`}
              disabled={Boolean(busy) || !agent.os_supported}
              title={item.verb}
              onClick={() => {
                if (needsConfirm && !isConfirming) {
                  setConfirming(item.action);
                  setNotice(`${item.label} changes the installed binary. Press again to confirm.`);
                  return;
                }
                void run(item.action, () =>
                  api(`/api/agents/${agent.kind}/${item.action}`, {
                    method: "POST",
                    body: JSON.stringify(
                      item.action === "update"
                        ? { approve: true }
                        : item.action === "install"
                          ? { force: true }
                          : {}
                    )
                  })
                );
              }}
            >
              {busy === item.action ? `${item.label}…` : isConfirming ? "Confirm" : item.label}
            </button>
          );
        })}
        <button
          className="btn small-btn primary"
          disabled={Boolean(busy) || !capability?.interactive_login}
          title={
            capability?.interactive_login
              ? "Opens the CLI's own login command in an embedded terminal"
              : capability?.limitation || "The CLI publishes no supported interactive login"
          }
          onClick={() => setTerminal({ account: null, provider: "" })}
        >
          {auth?.running_session ? "Open live sign-in" : "Login"}
        </button>
        <button
          className="btn small-btn"
          disabled={Boolean(busy)}
          onClick={() =>
            void run("models", () => api<AgentModelList>(`/api/agents/${agent.kind}/models`), (value) =>
              setModels(value as AgentModelList)
            )
          }
        >
          {busy === "models" ? "Reading models…" : "Check models"}
        </button>
        <button
          className="btn small-btn"
          disabled={Boolean(busy)}
          onClick={() => void run("sync", () => api(`/api/agents/${agent.kind}/accounts/sync`, { method: "POST" }))}
          title="Re-read accounts from the CLI's own status output"
        >
          {busy === "sync" ? "Re-reading…" : "Refresh accounts"}
        </button>
        <button
          className="btn small-btn"
          disabled={Boolean(busy)}
          onClick={() =>
            void run(
              "logout",
              () =>
                api(`/api/agents/${agent.kind}/logout`, {
                  method: "POST",
                  body: JSON.stringify({ provider: "" })
                }),
              (value: any) => setNotice(typeof value === "string" ? value : JSON.stringify(value))
            )
          }
          title={
            capability?.logout
              ? "Runs the CLI's documented logout"
              : capability?.limitation || "Logout support has not been verified"
          }
        >
          {busy === "logout" ? "Signing out…" : "Logout"}
        </button>
        <button
          className="btn small-btn"
          disabled={Boolean(busy)}
          onClick={() =>
            void run("logs", () => api<AgentLogText>(`/api/agents/${agent.kind}/logs`), (value) =>
              setLogs(value as AgentLogText)
            )
          }
        >
          {busy === "logs" ? "Loading…" : "View logs"}
        </button>
        <button
          className="btn small-btn"
          disabled={Boolean(busy)}
          onClick={() =>
            void run("enabled", () =>
              api(`/api/agents/${agent.kind}/enabled`, {
                method: "POST",
                body: JSON.stringify({ enabled: !agent.enabled })
              })
            )
          }
        >
          {agent.enabled ? "Disable agent" : "Enable agent"}
        </button>
      </div>

      {notice && <p className="info-box">{notice}</p>}

      {terminal && capability?.interactive_login && (
        <HostedTerminal
          agent={agent}
          account={terminal.account}
          provider={terminal.provider}
          resumeSessionId={auth?.running_session?.id ?? null}
          onSettled={async () => {
            // The server re-reads the CLI's status after the process exits; this
            // asks for it again so the table never shows the pre-login state.
            try {
              await api(`/api/agents/${agent.kind}/accounts/sync`, { method: "POST" });
            } catch (caught) {
              setError(caught instanceof ApiError ? caught.message : String(caught));
            }
            await reloadAuth();
            onChanged();
          }}
        />
      )}
      {terminal && (
        <div className="term-launch">
          {capability?.account_isolation === "config_dir" && (
            <label className="field">
              <span>Sign into account</span>
              <select
                value={terminal.account?.id ?? ""}
                onChange={(event) => {
                  const found = (auth?.accounts || []).find(
                    (item) => String(item.id) === event.target.value
                  );
                  setTerminal({ account: found || null, provider: terminal.provider });
                }}
              >
                <option value="">default config root</option>
                {(auth?.accounts || []).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                    {item.config_dir ? ` (isolated)` : ""}
                  </option>
                ))}
              </select>
              <small>
                {capability.multiple_accounts
                  ? "Each named account gets its own config directory, so two accounts can stay signed in at once."
                  : "This CLI holds one credential, so the default root is the only target."}
              </small>
            </label>
          )}
          {capability?.provider_selection && (
            <label className="field">
              <span>Provider (optional)</span>
              <input
                value={terminal.provider}
                placeholder={capability.limitation || "leave blank to let the CLI ask"}
                onChange={(event) =>
                  setTerminal({ account: terminal.account, provider: event.target.value })
                }
              />
            </label>
          )}
          <button className="btn small-btn" onClick={() => setTerminal(null)}>
            Close sign-in panel
          </button>
        </div>
      )}

      {models && (
        <div className="model-chips">
          {models.error ? (
            <p className="warn-box">{models.error}</p>
          ) : models.models.length === 0 ? (
            <p className="warn-box">
              The CLI returned no model list{models.auth_status === "unauthenticated" ? " because it is not signed in" : ""}.
            </p>
          ) : (
            models.models.map((model) => <span className="chip chip-muted" key={model}>{model}</span>)
          )}
        </div>
      )}

      {logs && (
        <div className="agent-log">
          <small className="mono">{logs.path || "server-recorded install log"}</small>
          <pre className="terminal">{logs.log || logs.stored_tail || "no output recorded"}</pre>
        </div>
      )}

      <div className="account-block">
        <header className="card-head">
          <div className="card-title">
            <div>
              <h3>Accounts ({auth?.accounts.length ?? 0})</h3>
              <p>
                {capability?.multiple_accounts
                  ? "Isolated through the CLI's documented config root."
                  : "This CLI keeps one credential per provider - the list is what it really holds."}
              </p>
            </div>
          </div>
        </header>
        <form
          className="row"
          onSubmit={(event) => {
            event.preventDefault();
            const name = newAccount.trim();
            if (!name) return;
            void run("add-account", () =>
              api(`/api/agents/${agent.kind}/accounts`, {
                method: "POST",
                body: JSON.stringify({ name })
              })
            ).then((ok) => {
              if (ok) setNewAccount("");
            });
          }}
        >
          <label className="field">
            <span>Add account name</span>
            <input
              value={newAccount}
              onChange={(event) => setNewAccount(event.target.value)}
              placeholder={capability?.multiple_accounts ? "e.g. work" : "refused unless the CLI supports isolation"}
              maxLength={60}
            />
          </label>
          <button className="btn small-btn" type="submit" disabled={Boolean(busy) || !newAccount.trim()}>
            {busy === "add-account" ? "Creating…" : "Add account"}
          </button>
        </form>

        {(auth?.accounts || []).length === 0 ? (
          <Empty
            title="No account read from this CLI"
            detail="Refresh accounts re-reads the CLI's own status output; nothing is invented here."
          />
        ) : (
          <div className="account-table">
            <div className="account-row head">
              <span>Account</span>
              <span>Session</span>
              <span>Last verified</span>
              <span>Actions</span>
            </div>
            {(auth?.accounts || []).map((item) => (
              <div key={item.id} className={`account-row${item.active ? " is-active" : ""}`}>
                <div className="account-id">
                  <strong>
                    {item.name}
                    {item.active && <em className="tag-active">active</em>}
                  </strong>
                  <small className="mono">
                    {item.source}
                    {item.config_dir ? ` · ${item.config_dir}` : ""}
                  </small>
                  {item.detail && <small>{item.detail}</small>}
                </div>
                <div className="account-state">
                  <StatusChip
                    status={item.status}
                    label={item.status === "signed_in" ? "signed in" : item.status.replace(/_/g, " ")}
                    tone={item.status === "signed_in" ? "ok" : item.status === "unknown" ? "muted" : "warn"}
                  />
                </div>
                <div className="account-state">
                  <small>{formatTime(item.last_verified_at)}</small>
                </div>
                <div className="account-actions">
                  <button
                    className="btn small-btn"
                    disabled={Boolean(busy) || item.active || !item.switchable || item.status !== "signed_in"}
                    title={
                      item.status !== "signed_in"
                        ? "Only an account the CLI reports as signed in can be made active"
                        : capability?.limitation || ""
                    }
                    onClick={() =>
                      void run(`switch:${item.id}`, () =>
                        api(`/api/agents/${agent.kind}/accounts/${item.id}/active`, { method: "POST" })
                      )
                    }
                  >
                    {busy === `switch:${item.id}` ? "Switching…" : "Switch to"}
                  </button>
                  <button
                    className="btn small-btn"
                    disabled={Boolean(busy)}
                    onClick={() => setTerminal({ account: item, provider: "" })}
                    title="Run the CLI's login for this account"
                  >
                    Login
                  </button>
                  <button
                    className={`btn small-btn${confirmDelete === item.id ? " primary" : ""}`}
                    disabled={Boolean(busy)}
                    onClick={() => {
                      if (confirmDelete !== item.id) {
                        setConfirmDelete(item.id);
                        setNotice(
                          "This forgets the account in ForgeMesh. Press again to confirm; an isolated config directory is only removed if you also purge it."
                        );
                        return;
                      }
                      setConfirmDelete(null);
                      void run(`delete:${item.id}`, () =>
                        api(`/api/agents/${agent.kind}/accounts/${item.id}/delete`, {
                          method: "POST",
                          body: JSON.stringify({ confirm: true, purge: Boolean(item.config_dir) })
                        })
                      );
                    }}
                  >
                    {busy === `delete:${item.id}`
                      ? "Removing…"
                      : confirmDelete === item.id
                        ? "Confirm remove"
                        : "Remove"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        {(auth?.history || []).length > 0 && (
          <div className="session-history">
            <h3>Recent sign-in sessions</h3>
            {auth!.history.slice(0, 5).map((row) => (
              <div className="terminal-line muted" key={row.id}>
                <span className="terminal-id mono">{row.id}</span>
                <span className="terminal-time">{formatTime(row.ended_at || row.started_at)}</span>
                <StatusChip
                  status={row.status === "exited" && row.exit_code === 0 ? "completed" : row.status}
                  tone={row.status === "exited" && row.exit_code === 0 ? "ok" : row.status === "running" ? "info" : "muted"}
                />
                <span className="terminal-data mono">{row.detail || row.tail.split("\n").pop() || ""}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}
