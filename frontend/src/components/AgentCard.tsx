import type { AgentInfo } from "../types";
import { AvailabilityChip, StatusChip } from "./ui";

const STEPS = ["Detect", "Download", "Install", "Configure", "Verify", "Ready"];

function stepIndex(agent: AgentInfo): number {
  if (agent.install_status === "ready") return 5;
  if (agent.install_status === "failed") {
    const stage = agent.install_stage || "installing";
    return Math.max(1, STEPS.findIndex((item) => item.toLowerCase() === stage));
  }
  if (agent.install_status === "verifying") return 4;
  if (agent.install_status === "installing") {
    if (agent.install_stage === "downloading") return 1;
    if (agent.install_stage === "configuring") return 3;
    if (agent.install_stage === "verifying") return 4;
    if (agent.install_stage === "detected") return 0;
    return 2;
  }
  if (agent.install_status === "detected") return 0;
  return -1;
}

export function AgentCard({
  agent,
  busy,
  onAction,
  onLogs,
  onManage,
  compact = false
}: {
  agent: AgentInfo;
  busy?: string;
  onAction: (action: "install" | "verify" | "detect" | "reinstall") => void;
  onLogs: () => void;
  onManage?: () => void;
  compact?: boolean;
}) {
  const current = stepIndex(agent);
  const failed = agent.install_status === "failed";
  const message =
    agent.metadata?.last_stage_message ||
    (failed ? agent.last_error : "") ||
    (agent.install_status === "ready" ? "Executable and interface verified." : "Waiting.");

  return (
    <article className={`agent-card ${failed ? "agent-failed" : ""} ${compact ? "compact" : ""}`}>
      <header className="agent-card-head">
        <div className="agent-ident">
          <div className={`agent-logo ${agent.kind}`}>{agent.kind === "opencode" ? "OC" : "Q"}</div>
          <div>
            <h3>{agent.display_name}</h3>
            <span className="muted small">Central server only</span>
          </div>
        </div>
        <StatusChip status={agent.install_status} />
      </header>

      <div className="agent-facts">
        <div>
          <span>Version</span>
          <strong>{agent.version || "—"}</strong>
        </div>
        <div>
          <span>Executable</span>
          <strong className="mono ellipsis" title={agent.path}>
            {agent.path || "—"}
          </strong>
        </div>
        <div>
          <span>Authentication</span>
          <StatusChip status={agent.auth_status} />
        </div>
        <div>
          <span>Availability</span>
          <AvailabilityChip
            status={agent.availability}
            functional={agent.metadata?.functional_check}
          />
        </div>
      </div>

      <div className="agent-steps" aria-label="Installation stages">
        {STEPS.map((step, index) => {
          const state =
            failed && index === Math.max(current, 0)
              ? "failed"
              : current > index
                ? "done"
                : current === index
                  ? "active"
                  : "pending";
          return (
            <div key={step} className={`agent-step ${state}`}>
              <i>{state === "done" ? "✓" : state === "failed" ? "!" : index + 1}</i>
              <span>{step}</span>
            </div>
          );
        })}
      </div>

      <p className={`agent-message ${failed ? "bad" : ""}`}>{message}</p>

      {agent.metadata?.functional_check && (
        <p className={`agent-check ${agent.metadata.functional_check.verified ? "ok" : "warn"}`}>
          {agent.metadata.functional_check.verified
            ? `Real prompt verified${agent.metadata.functional_check.model ? ` · ${agent.metadata.functional_check.model}` : ""}`
            : agent.metadata.functional_check.auth_required
              ? "Executable verified · authentication required for real prompts"
              : `Functional check: ${agent.metadata.functional_check.detail || "not successful"}`}
        </p>
      )}

      {agent.metadata?.auth_detail && (
        <p className="agent-auth-detail">{agent.metadata.auth_detail}</p>
      )}
      {failed && agent.last_error && <p className="error-box">{agent.last_error}</p>}

      <footer className="agent-actions">
        {agent.install_status === "ready" ? (
          <>
            <button className="btn" disabled={!!busy} onClick={() => onAction("verify")}>
              {busy === "verify" ? "Verifying…" : "Verify installation"}
            </button>
            <button className="btn ghost" disabled={!!busy} onClick={onLogs}>
              View logs
            </button>
          </>
        ) : failed ? (
          <>
            <button className="btn danger" disabled={!!busy} onClick={() => onAction("install")}>
              {busy === "install" ? "Retrying…" : "Retry installation"}
            </button>
            <button className="btn ghost" disabled={!!busy} onClick={onLogs}>
              View logs
            </button>
          </>
        ) : (
          <>
            <button className="btn" disabled={!!busy} onClick={() => onAction("install")}>
              {busy === "install" ? "Starting…" : "Install now"}
            </button>
            <button className="btn ghost" disabled={!!busy} onClick={() => onAction("detect")}>
              Detect
            </button>
          </>
        )}
        {onManage && (
          <button className="btn ghost" onClick={onManage}>
            Manage
          </button>
        )}
      </footer>
    </article>
  );
}
