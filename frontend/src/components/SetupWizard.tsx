import type { AgentInfo, ServerStatus, SetupStatus } from "../types";
import { AgentCard } from "./AgentCard";
import type { AiAccountSummary } from "../AiAccountsPage";

/**
 * Only reachable from the Advanced disclosure: these are the CLI's own
 * documented commands, shown for operators who want them, never as the
 * primary path. Sign-in itself is hosted in Settings -> Coding Agents.
 */
const AUTH_COMMANDS: Record<string, string> = {
  opencode: "opencode providers login",
  qoder: "qodercli login"
};

type StepState = "done" | "active" | "todo" | "blocked";

interface Step {
  title: string;
  optional?: boolean;
  state: StepState;
  detail: string;
  action?: { label: string; run: () => void };
  error?: string;
}

const STATE_LABEL: Record<StepState, string> = {
  done: "Done",
  active: "In progress",
  todo: "Next",
  blocked: "Blocked"
};

export function SetupWizard({
  setup,
  status,
  accounts,
  onInstall,
  onVerify,
  onDetect,
  onLogs,
  onContinue,
  onOpenSettings,
  onRefresh,
  busy,
  canClose
}: {
  setup: SetupStatus;
  status: ServerStatus | null;
  accounts: AiAccountSummary | null;
  onInstall: (kind: string) => void;
  onVerify: (kind: string) => void;
  onDetect: (kind: string) => void;
  onLogs: (kind: string) => void;
  onContinue: () => void;
  onOpenSettings: (section: "coding-agents" | "ai-accounts") => void;
  onRefresh: () => void;
  busy: Record<string, string>;
  canClose: boolean;
}) {
  const agents = Object.values(setup.agents);
  const running = setup.state === "setting_up" || setup.pending.length > 0;
  const done = setup.complete && !running;
  const labelOf = (kind: string) => setup.agents[kind]?.display_name || kind;
  const opencode = agents.find((agent) => agent.kind === "opencode") || null;
  const qoder = agents.find((agent) => agent.kind === "qoder") || null;
  const verifiedAccounts = accounts
    ? accounts.providers.reduce((sum, provider) => sum + provider.authenticated, 0)
    : 0;

  const steps: Step[] = [
    {
      title: "Welcome to ForgeMesh",
      state: "done",
      detail: "The central server is answering, so this window is reading real state - not a mock."
    },
    {
      title: "Check system compatibility",
      state: agents.some((agent) => !agent.os_supported)
        ? "blocked"
        : agents.length > 0
          ? "done"
          : "todo",
      detail: agents.length
        ? `${agents.length} coding agent(s) checked on this platform.`
        : "Waiting for the server to report the platform check."
    },
    {
      title: "Install required components",
      state: status?.browser.connected ? "done" : "active",
      detail: status?.browser.connected
        ? "ForgeMesh Browser runtime is reachable, so provider logins can open a real window."
        : "ForgeMesh Browser runtime is not reachable, so provider login windows cannot open yet.",
      action: { label: "Check again", run: () => void onRefresh() },
      error: status?.browser.error || undefined
    },
    ...[opencode, qoder].map(
      (agent): Step => ({
        title: `Install ${agent?.display_name || "coding agent"}`,
        state: !agent
          ? "todo"
          : agent.install_status === "ready"
            ? "done"
            : agent.install_status === "failed"
              ? "blocked"
              : ["installing", "verifying", "detected"].includes(agent.install_status)
                ? "active"
                : "todo",
        detail: !agent
          ? "The server has not reported this CLI yet."
          : agent.install_status === "ready"
            ? `Installed${agent.version ? ` · version ${agent.version}` : ""}.`
            : agent.install_status === "installing"
              ? agent.install_stage || "Downloading from the official source…"
              : agent.install_status === "detected"
                ? "Found on this machine but not verified."
                : agent.install_status === "verifying"
                  ? "Running the server's own checks…"
                  : agent.installed
                    ? "Present, but the server has not verified it."
                    : "Not installed on this central server.",
        action: !agent
          ? undefined
          : agent.install_status === "ready"
            ? { label: "Verify again", run: () => onVerify(agent.kind) }
            : agent.installed || agent.install_status === "detected"
              ? { label: "Verify", run: () => onVerify(agent.kind) }
              : agent.install_status === "not_detected"
                ? { label: "Detect", run: () => onDetect(agent.kind) }
                : { label: agent.install_status === "failed" ? "Retry install" : "Install", run: () => onInstall(agent.kind) },
        error: agent?.last_error || undefined
      })
    ),
    {
      title: "Configure central server",
      state: status?.version ? "done" : "active",
      detail: status?.version
        ? `${status.service || "ForgeMesh control"} is running and this dashboard is authenticated.`
        : "Waiting for the server to report itself.",
      action: { label: "Re-read", run: () => void onRefresh() }
    },
    {
      title: "Connect AI provider accounts",
      optional: true,
      state: !accounts
        ? "todo"
        : !accounts.connected
          ? "blocked"
          : accounts.totals.accounts > 0
            ? "done"
            : "todo",
      detail: accounts?.connected
        ? accounts.totals.accounts > 0
          ? `${accounts.totals.accounts} account(s) registered, ${accounts.totals.authenticated} signed in, across ${accounts.totals.providers_with_accounts} provider(s).`
          : "No provider account registered yet. You can add them any time, or skip and finish."
        : "Reading the account roster from the browser runtime…",
      action: { label: "Add accounts", run: () => onOpenSettings("ai-accounts") },
      error: accounts && !accounts.connected ? accounts.error || undefined : undefined
    },
    {
      title: "Authenticate coding agents",
      state: agents.length > 0 && setup.auth_needed.length === 0 ? "done" : "todo",
      detail: setup.auth_needed.length
        ? `Still needs its own sign-in: ${setup.auth_needed.map(labelOf).join(", ")}. Installing a CLI does not sign it in.`
        : setup.auth_optional.length
          ? `${setup.auth_optional.map(labelOf).join(", ")} answers real prompts with no credential stored, so signing in there is optional.`
          : agents.length
            ? "Every detected agent reports its own authenticated status."
            : "Nothing to authenticate until an agent is installed.",
      action: { label: "Sign in", run: () => onOpenSettings("coding-agents") }
    },
    {
      title: "Verify connections",
      state:
        agents.length > 0 && setup.available.length === agents.length
          ? "done"
          : verifiedAccounts > 0 || setup.available.length > 0
            ? "active"
            : "todo",
      detail: `${setup.available.length} of ${agents.length} agent(s) answered an availability check${
        setup.limited.length ? `; limited: ${setup.limited.map(labelOf).join(", ")}` : ""
      }${setup.unverified.length ? `; not yet verified: ${setup.unverified.map(labelOf).join(", ")}` : ""}. ${verifiedAccounts} provider account session(s) verified.`,
      action: agents.length
        ? {
            label: "Verify all agents",
            run: () => agents.forEach((agent) => onVerify(agent.kind))
          }
        : undefined
    },
    {
      title: "Finish setup",
      state: "todo",
      detail: ""
    }
  ];

  const openSteps = steps.slice(0, 9).filter((step) => step.state !== "done");
  const names = openSteps.map((step) => step.title).join(", ");
  const checklistClear = openSteps.length === 0;
  const last = steps[steps.length - 1];
  last.state = checklistClear ? "done" : setup.first_run_complete ? "todo" : "active";
  last.detail = openSteps.length
    ? setup.first_run_complete
      ? `First-run was already closed on the server, but ${openSteps.length} step(s) are still open: ${names}.`
      : `Still open: ${names}. Nothing here reports success until the real command succeeds.`
    : "Every step the server reports is done. Opening the dashboard is safe.";

  return (
    <div className="wizard-backdrop">
      <div className="wizard">
        <header className="wizard-head">
          <div>
            <span className="eyebrow">First-run setup · central server only</span>
            <h1>Getting ForgeMesh ready</h1>
            <p>
              ForgeMesh detects and installs OpenCode CLI and Qoder CLI from their official
              sources, then registers your own provider accounts. Installation, verification and
              sign-in are reported separately: nothing is marked ready until the real command
              succeeds. You can skip the optional steps and finish them later.
            </p>
          </div>
          <div className="wizard-state">
            <StatusBadge setup={setup} />
            {canClose && (
              <button className="btn ghost" onClick={onContinue}>
                Close
              </button>
            )}
          </div>
        </header>

        {setup.running && setup.step && <p className="info-box">Server is working: {setup.step}</p>}

        <ol className="wizard-checklist">
          {steps.map((step, index) => (
            <li key={step.title} className={`wz-row wz-${step.state}`}>
              <i className="wz-index">{step.state === "done" ? "✓" : index + 1}</i>
              <div className="wz-body">
                <strong>
                  {step.title}
                  <em className={step.optional ? "wz-tag optional" : "wz-tag"}>
                    {step.optional ? "optional" : "required"}
                  </em>
                </strong>
                <p>{step.detail}</p>
                {step.error && <p className="error-box">{step.error}</p>}
              </div>
              <div className="wz-end">
                <span className={`chip chip-${step.state === "done" ? "ok" : step.state === "blocked" ? "bad" : step.state === "active" ? "info" : "muted"}`}>
                  {STATE_LABEL[step.state]}
                </span>
                {step.action && (
                  <button className="btn small-btn" onClick={step.action.run}>
                    {step.action.label}
                  </button>
                )}
              </div>
            </li>
          ))}
        </ol>

        <details className="wizard-advanced">
          <summary>Advanced settings</summary>
          <p className="muted small">
            Technical detail for operators: install logs, file paths, ports and the CLI's own
            commands. Sign-in happens in Settings → Coding Agents, where the CLI's login command
            runs in an embedded terminal - you never type a password here.
          </p>
          <div className="wizard-grid">
            {agents.map((agent) => (
              <AgentCard
                key={agent.kind}
                agent={agent}
                compact
                busy={busy[agent.kind]}
                onAction={(action) => {
                  if (action === "detect") onDetect(agent.kind);
                  else if (action === "verify") onVerify(agent.kind);
                  else onInstall(agent.kind);
                }}
                onLogs={() => onLogs(agent.kind)}
              />
            ))}
          </div>
          <div className="auth-commands">
            {agents.map((agent: AgentInfo) => (
              <div key={agent.kind} className="auth-command">
                <div>
                  <strong>{agent.display_name}</strong>
                  <span className={`small ${agent.auth_status === "authenticated" ? "ok" : "muted"}`}>
                    {agent.auth_status === "authenticated"
                      ? "Signed in"
                      : agent.auth_status === "unauthenticated"
                        ? "Not signed in"
                        : "Status unknown"}
                  </span>
                </div>
                {agent.auth_status !== "authenticated" && <code>{AUTH_COMMANDS[agent.kind]}</code>}
                <small className="mono">{agent.path || "path not detected"}</small>
                <button className="btn ghost small-btn" onClick={() => onVerify(agent.kind)}>
                  Re-check
                </button>
              </div>
            ))}
          </div>
          <ul className="wz-facts mono">
            <li>server: {status ? `${status.service || "forgemesh-control"} ${status.version}` : "unknown"}</li>
            <li>browser runtime: {status?.browser.url || "unknown"}</li>
            <li>browser runtime reachable: {String(Boolean(status?.browser.connected))}</li>
          </ul>
        </details>

        {setup.missing_capabilities.length > 0 && (
          <section className="wizard-warnings">
            <strong>Unavailable capabilities</strong>
            <ul>
              {setup.missing_capabilities.map((item) => (
                <li key={item.kind}>
                  <b>{item.kind}</b>: {item.reason}
                </li>
              ))}
            </ul>
          </section>
        )}

        <footer className="wizard-foot">
          <span className="muted small">
            {running
              ? "Installation runs on this central server. You can keep the window open."
              : done
                ? checklistClear
                  ? "Setup is complete. Sign-in and provider accounts stay adjustable in Settings."
                  : `${openSteps.length} checklist step(s) still need you: ${names}. The dashboard is safe to open - every one of these actions stays available in Settings.`
                : "Setup is not complete yet. Steps marked blocked show the server's own error."}
          </span>
          <div className="row">
            {setup.failed.length > 0 && (
              <button className="btn" onClick={() => setup.failed.forEach(onInstall)}>
                Retry failed
              </button>
            )}
            {!done && (
              <button className="btn" onClick={onContinue}>
                Finish later and open dashboard
              </button>
            )}
            {done && (
              <button className="btn primary" onClick={onContinue}>
                {setup.state === "ready" ? "Open dashboard" : "Continue in degraded mode"}
              </button>
            )}
          </div>
        </footer>
      </div>
    </div>
  );
}

function StatusBadge({ setup }: { setup: SetupStatus }) {
  if (setup.state === "setting_up")
    return (
      <span className="builder-badge working">
        <i /> Installing…
      </span>
    );
  if (setup.state === "ready")
    return (
      <span className="builder-badge ok">
        <i /> Both agents ready
      </span>
    );
  if (setup.state === "degraded")
    return (
      <span className="builder-badge warn">
        <i /> Degraded mode available
      </span>
    );
  if (setup.state === "failed")
    return (
      <span className="builder-badge bad">
        <i /> Installation failed
      </span>
    );
  return (
    <span className="builder-badge">
      <i /> Needs setup
    </span>
  );
}
