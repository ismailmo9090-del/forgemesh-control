import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useApp } from "../context";
import type { ProjectDetail } from "../types";
import {
  Button,
  Empty,
  Icon,
  Pill,
  SectionHeader,
  formatDate,
  timeAgo
} from "../components/ui";

const STAGES = ["blueprint", "ui_spec", "implementation"] as const;

export default function Approvals() {
  const { snapshot, refresh, busy, setBusy, toast, navigate } = useApp();
  const projects = snapshot.operator.projects;
  const [projectId, setProjectId] = useState(projects[0]?.id || "");
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [note, setNote] = useState("");
  const [decisionStage, setDecisionStage] = useState<string>("");
  const [approve, setApprove] = useState(true);
  const [error, setError] = useState("");

  const pending = useMemo(() => {
    const stages = detail?.approval_states || {};
    return STAGES.reduce((total, stage) => total + (stages[stage]?.state === "pending" ? 1 : 0), 0);
  }, [detail]);

  useEffect(() => {
    if (!projectId && projects[0]) setProjectId(projects[0].id);
  }, [projects, projectId]);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setError("");
    api
      .project(projectId)
      .then((payload) => !cancelled && setDetail(payload as ProjectDetail))
      .catch((err: Error) => !cancelled && setError(err.message));
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const reload = async () => {
    await refresh();
    if (projectId) setDetail((await api.project(projectId)) as ProjectDetail);
  };

  const openGate = async () => {
    setBusy("gate");
    setError("");
    try {
      const result = (await api.gate(projectId, "Review requested from the client manager.")) as Record<string, unknown>;
      const created = Array.isArray((result as { created?: unknown[] }).created) ? (result as { created: unknown[] }).created.length : null;
      toast(created != null ? `Gate processed; ${created} new pending row(s) written by the server.` : "Gate processed by the server.", "ok");
      await reload();
    } catch (err: unknown) {
      setError((err as Error).message);
      toast((err as Error).message, "bad");
    } finally {
      setBusy("");
    }
  };

  const decide = async () => {
    const stage = detail?.approval_states?.[decisionStage];
    if (!stage?.approval_id) {
      toast("This stage has no approval row to decide.", "warn");
      return;
    }
    setBusy("decide");
    setError("");
    try {
      await api.decide(projectId, stage.approval_id, approve, note.trim());
      toast(approve ? "Stage approved on the server." : "Changes requested; the note is the durable feedback.", approve ? "ok" : "warn");
      setDecisionStage("");
      setNote("");
      await reload();
    } catch (err: unknown) {
      setError((err as Error).message);
      toast((err as Error).message, "bad");
    } finally {
      setBusy("");
    }
  };

  if (!snapshot.operator.available) {
    return (
      <div className="page">
        <div className="locked-page card">
          <Icon name="shield" size={30} />
          <h2>Approvals need operator mode</h2>
          <p>
            Approval rows live behind the server control token. The worker token cannot read or write
            them. This protects the gate from a compromised worker.
          </p>
          <Button icon="settings" onClick={() => navigate("settings")}>Open settings</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <SectionHeader
        title="Human gates"
        subtitle="Opening a gate or deciding a stage writes to the server. Nothing is approved automatically."
        action={
          <div className="row">
            <select value={projectId} onChange={(event) => { setProjectId(event.target.value); setDetail(null); }}>
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
            <Button size="sm" variant="secondary" icon="refresh" onClick={() => void reload()}>Reload</Button>
          </div>
        }
      />

      {error && <div className="inline-alert bad"><Icon name="alert" size={17} /><span>{error}</span></div>}

      {!projectId ? (
        <Empty title="No projects to review" detail="Create a project on the server first." />
      ) : (
        <>
          <div className="gate-summary">
            <div>
              <span className="eyebrow">Selected project</span>
              <h2>{detail?.name || projectId}</h2>
              <p>{detail?.description || "No description."}</p>
            </div>
            <div className="gate-score">
              <strong>{pending}</strong>
              <span>pending stage{pending === 1 ? "" : "s"}</span>
            </div>
            <Button icon="gate" onClick={openGate} disabled={busy === "gate"}>
              {busy === "gate" ? "Opening…" : "Open review gate"}
            </Button>
          </div>

          <div className="gate-grid">
            {STAGES.map((stage) => {
              const value = detail?.approval_states?.[stage];
              const state = value?.state || "missing";
              return (
                <section key={stage} className={`gate-card ${state}`}>
                  <div className="gate-card-head">
                    <div>
                      <span className="eyebrow">{stage.replace("_", " ")}</span>
                      <h3>{value?.ui_state || "Not requested"}</h3>
                    </div>
                    <Pill tone={state === "approved" ? "green" : state === "pending" ? "amber" : state === "rejected" ? "red" : "neutral"}>
                      {state}
                    </Pill>
                  </div>
                  <p>{value?.summary || "The server has no approval summary for this stage."}</p>
                  {value?.requested_at && <span className="small muted">Requested {formatDate(value.requested_at)}</span>}
                  {value?.note && <div className="gate-note"><strong>Decision note</strong><span>{value.note}</span></div>}
                  <div className="row">
                    {state === "pending" ? (
                      <>
                        <Button size="sm" variant="success" icon="check" onClick={() => { setDecisionStage(stage); setApprove(true); setNote(""); }}>
                          Approve
                        </Button>
                        <Button size="sm" variant="secondary" icon="alert" onClick={() => { setDecisionStage(stage); setApprove(false); setNote(""); }}>
                          Request changes
                        </Button>
                      </>
                    ) : (
                      <span className="small muted">{state === "missing" ? "Open the gate to create pending rows for stages that need one." : "No action available for this state."}</span>
                    )}
                  </div>
                  {value?.history && value.history.length > 1 && (
                    <details className="gate-history">
                      <summary>{value.history.length} decisions in history</summary>
                      {value.history.map((row) => (
                        <div key={row.id} className="gate-history-row">
                          <Pill tone={row.status === "approved" ? "green" : row.status === "pending" ? "amber" : "red"}>{row.status}</Pill>
                          <span className="small muted">{timeAgo(row.requested_at)}</span>
                          <span className="small ellipsis">{row.note || row.summary}</span>
                        </div>
                      ))}
                    </details>
                  )}
                </section>
              );
            })}
          </div>

          {decisionStage && (
            <section className="card decision-card">
              <div className="card-head">
                <div>
                  <h2>{approve ? "Approve" : "Request changes"} · {decisionStage.replace("_", " ")}</h2>
                  <p>
                    {approve
                      ? "This sends POST /api/projects/{id}/approvals/{approvalId}/decide with approved=true."
                      : "There is no separate change-request resource. The note is stored on the approval decision and the server's review report understands it."}
                  </p>
                </div>
                <Button size="sm" variant="ghost" icon="close" onClick={() => setDecisionStage("")}>Cancel</Button>
              </div>
              <div className="field">
                <label>{approve ? "Approval note (optional)" : "What must change?"}</label>
                <textarea
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  rows={4}
                  placeholder={approve ? "Reviewed on the output laptop." : "Describe the concrete change. This is durable feedback on the decision row."}
                />
                <small>Decision note is sent verbatim; the server records it on the approval row.</small>
              </div>
              <div className="row">
                <Button
                  variant={approve ? "success" : "danger"}
                  icon={approve ? "check" : "alert"}
                  onClick={decide}
                  disabled={busy === "decide" || (!approve && !note.trim())}
                >
                  {busy === "decide" ? "Sending…" : approve ? "Approve stage" : "Request changes"}
                </Button>
                <span className="small muted">Approval ID {detail?.approval_states?.[decisionStage]?.approval_id || "—"}</span>
              </div>
            </section>
          )}

          <div className="grid-2">
            <section className="card">
              <div className="card-head"><div><h2>Decision history</h2><p>Every row returned by the project detail.</p></div></div>
              {(detail?.approvals || []).length === 0 ? (
                <p className="small muted">No approval rows yet.</p>
              ) : (
                <div className="timeline">
                  {detail?.approvals.map((row) => (
                    <div key={row.id} className="timeline-row">
                      <span className={`timeline-dot ${row.status}`} />
                      <div>
                        <div className="row">
                          <strong>{row.stage.replace("_", " ")}</strong>
                          <Pill tone={row.status === "approved" ? "green" : row.status === "pending" ? "amber" : "red"}>{row.status}</Pill>
                          <span className="small muted">{row.decided_at ? `decided ${timeAgo(row.decided_at)}` : `requested ${timeAgo(row.requested_at)}`}</span>
                        </div>
                        <p>{row.summary || "No summary."}</p>
                        {row.note && <span className="small muted">Note: {row.note}</span>}
                        <span className="mono small">{row.id}</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section className="card">
              <div className="card-head"><div><h2>Server-side review</h2><p>Generate the report after a decision.</p></div></div>
              <p className="small muted">
                The report is produced by the control server from its own task and approval rows. It is
                the artifact reviewers can attach to a release.
              </p>
              <Button
                variant="secondary"
                icon="logs"
                disabled={busy === "report"}
                onClick={async () => {
                  setBusy("report");
                  try {
                    await api.deliveryReport(projectId, "review");
                    toast("Review report generated by the server.", "ok");
                  } catch (err: unknown) {
                    toast((err as Error).message, "bad");
                  } finally {
                    setBusy("");
                  }
                }}
              >
                {busy === "report" ? "Generating…" : "Generate review report"}
              </Button>
              <div className="gap-note">
                <strong>What this screen will not do</strong>
                <ul>
                  <li>It will not turn a free-text change request into a task; the server exposes no such endpoint.</li>
                  <li>It will not auto-approve a stage because a task finished.</li>
                  <li>It will not read approvals with the worker token.</li>
                </ul>
              </div>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
