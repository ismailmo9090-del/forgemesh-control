import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useApp } from "../context";
import type {
  ArtifactRecord,
  DeliveryRun,
  ProjectDetail,
  WorkspaceDiff,
  WorkspaceStatus
} from "../types";
import {
  Button,
  Empty,
  Icon,
  Pill,
  Progress,
  SectionHeader,
  formatBytes,
  formatDate,
  shortSha,
  timeAgo
} from "../components/ui";

type Tab = "overview" | "workspace" | "artifacts" | "delivery" | "documents";

export default function Review() {
  const { snapshot, refresh, busy, setBusy, toast, navigate } = useApp();
  const projects = snapshot.operator.projects;
  const [projectId, setProjectId] = useState(projects[0]?.id || "");
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [status, setStatus] = useState<WorkspaceStatus | null>(null);
  const [diff, setDiff] = useState<WorkspaceDiff | null>(null);
  const [log, setLog] = useState<{ log?: string; entries?: unknown[] } | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactRecord[]>([]);
  const [runs, setRuns] = useState<DeliveryRun[]>([]);
  const [documents, setDocuments] = useState<Array<Record<string, unknown>>>([]);
  const [documentText, setDocumentText] = useState<{ id: string; body: string; title: string } | null>(null);
  const [command, setCommand] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState("");

  const selected = useMemo(() => projects.find((project) => project.id === projectId), [projects, projectId]);

  useEffect(() => {
    if (!projectId && projects[0]) setProjectId(projects[0].id);
  }, [projects, projectId]);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading("detail");
    setError("");
    api
      .project(projectId)
      .then((payload) => !cancelled && setDetail(payload as ProjectDetail))
      .catch((err: Error) => !cancelled && setError(err.message))
      .finally(() => !cancelled && setLoading(""));
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    void loadWorkspace(false);
    void loadArtifacts();
    void loadRuns();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const path = detail?.workspace_path || "";

  const loadWorkspace = async (announce = true) => {
    if (!path) return;
    setLoading("workspace");
    try {
      const [nextStatus, nextDiff, nextLog] = await Promise.all([
        api.workspaceStatus(path) as Promise<WorkspaceStatus>,
        api.workspaceDiff(path) as Promise<WorkspaceDiff>,
        api.workspaceLog(path) as Promise<{ log?: string; entries?: unknown[] }>
      ]);
      setStatus(nextStatus);
      setDiff(nextDiff);
      setLog(nextLog);
      if (announce) toast("Workspace status read from the server-side route.", "ok");
    } catch (err: unknown) {
      setError((err as Error).message);
    } finally {
      setLoading("");
    }
  };

  const loadArtifacts = async () => {
    if (!projectId) return;
    try {
      const payload = (await api.artifacts(projectId)) as { artifacts?: ArtifactRecord[] };
      setArtifacts(payload.artifacts || []);
    } catch (err: unknown) {
      setError((err as Error).message);
    }
  };

  const loadRuns = async () => {
    if (!projectId) return;
    try {
      const payload = (await api.runs(projectId)) as { runs?: DeliveryRun[] };
      setRuns(payload.runs || []);
    } catch (err: unknown) {
      setError((err as Error).message);
    }
  };

  const loadDocuments = async () => {
    try {
      const payload = (await api.documents()) as { documents?: Array<Record<string, unknown>> };
      setDocuments(payload.documents || []);
    } catch (err: unknown) {
      setError((err as Error).message);
    }
  };

  useEffect(() => {
    if (tab === "documents" && documents.length === 0) void loadDocuments();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  if (!snapshot.operator.available) {
    return (
      <div className="page">
        <div className="locked-page card">
          <Icon name="shield" size={30} />
          <h2>Review screens need operator mode</h2>
          <p>
            The worker token cannot read <span className="mono">/api/projects</span>,{" "}
            <span className="mono">/api/tasks</span> or <span className="mono">/api/delivery</span>. This is
            a server security invariant, not a client limitation. Enable operator mode with the server
            control token to host the output-laptop review workflow.
          </p>
          <div className="row">
            <Button icon="settings" onClick={() => navigate("settings")}>Enable in Settings</Button>
            <Button variant="secondary" icon="refresh" onClick={() => void refresh()}>Re-check</Button>
          </div>
          <div className="gap-note">
            <strong>Known server gaps</strong>
            <ul>
              {snapshot.operator.protocol_gaps.map((gap) => <li key={gap}>{gap}</li>)}
            </ul>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <SectionHeader
        title="Output laptop"
        subtitle="Read projects, inspect a real workspace and verify delivery evidence. Actions only run when a human presses them."
        action={
          <div className="row">
            <select value={projectId} onChange={(event) => { setProjectId(event.target.value); setDetail(null); }}>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>{project.name} · {project.id}</option>
              ))}
            </select>
            <Button size="sm" variant="secondary" icon="refresh" onClick={async () => {
              await refresh();
              if (projectId) {
                const next = (await api.project(projectId)) as ProjectDetail;
                setDetail(next);
                await Promise.all([loadArtifacts(), loadRuns()]);
                toast("Project refreshed from the server.", "ok");
              }
            }}>Refresh</Button>
          </div>
        }
      />

      {error && (
        <div className="inline-alert bad">
          <Icon name="alert" size={17} />
          <span>{error}</span>
          <button className="link-btn" onClick={() => setError("")}>dismiss</button>
        </div>
      )}

      {!projectId ? (
        <Empty
          title="No project on this server"
          detail="Create a project in the control dashboard first. The Client Manager does not invent projects or tasks."
          action={<Button size="sm" variant="secondary" icon="refresh" onClick={() => void refresh()}>Refresh operator view</Button>}
        />
      ) : (
        <>
          <div className="project-hero">
            <div className="project-hero-main">
              <span className="eyebrow">Project</span>
              <h2>{detail?.name || selected?.name || projectId}</h2>
              <p>{detail?.description || selected?.description || "No description returned by the server."}</p>
              <div className="row">
                <Pill tone={detail?.status === "completed" ? "green" : detail?.status === "failed" ? "red" : "blue"}>
                  {detail?.status || selected?.status || "unknown"}
                </Pill>
                <span className="small muted">mode {detail?.mode || selected?.mode || "—"}</span>
                <span className="mono small">{projectId}</span>
              </div>
            </div>
            <div className="project-hero-stats">
              <div><strong>{detail?.tasks_total ?? selected?.tasks_total ?? 0}</strong><span>tasks</span></div>
              <div><strong>{detail?.tasks_completed ?? selected?.tasks_completed ?? 0}</strong><span>complete</span></div>
              <div><strong>{detail?.tasks_running ?? selected?.tasks_running ?? 0}</strong><span>running</span></div>
            </div>
            <div className="project-hero-progress">
              <span>Progress</span>
              <Progress value={Number(detail?.progress ?? selected?.progress ?? 0)} tone="blue" />
              <b>{Number(detail?.progress ?? selected?.progress ?? 0)}%</b>
            </div>
          </div>

          <div className="tabs">
            {(["overview", "workspace", "artifacts", "delivery", "documents"] as Tab[]).map((item) => (
              <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
                {item}
                {item === "artifacts" && artifacts.length > 0 && <b>{artifacts.length}</b>}
                {item === "delivery" && runs.length > 0 && <b>{runs.length}</b>}
              </button>
            ))}
          </div>

          {tab === "overview" && (
            <div className="grid-2">
              <section className="card">
                <div className="card-head">
                  <div><h2>Approval stages</h2><p>Read from the project detail; decisions happen in Approvals.</p></div>
                  <Button size="sm" variant="ghost" icon="arrow" onClick={() => navigate("approvals")}>Open gates</Button>
                </div>
                <div className="stage-cards">
                  {Object.entries(detail?.approval_states || {}).map(([stage, value]) => (
                    <div key={stage} className={`stage-card ${value.state}`}>
                      <div>
                        <strong>{stage.replace("_", " ")}</strong>
                        <span>{value.summary || "No request recorded."}</span>
                      </div>
                      <Pill tone={value.state === "approved" ? "green" : value.state === "pending" ? "amber" : value.state === "rejected" ? "red" : "neutral"}>
                        {value.ui_state}
                      </Pill>
                    </div>
                  ))}
                  {Object.keys(detail?.approval_states || {}).length === 0 && (
                    <p className="small muted">The project detail has no approval rows yet. Open the gate from Approvals when the work is ready for review.</p>
                  )}
                </div>
              </section>

              <section className="card">
                <div className="card-head">
                  <div><h2>Workspace</h2><p>Path returned by the server for this project.</p></div>
                  <Pill tone={path ? "blue" : "neutral"}>{path ? "configured" : "missing"}</Pill>
                </div>
                <dl className="kv">
                  <div><dt>Path</dt><dd className="mono ellipsis">{path || "—"}</dd></div>
                  <div><dt>Git repo</dt><dd className="mono ellipsis">{detail?.git_repo || "—"}</dd></div>
                  <div><dt>Created</dt><dd>{formatDate(detail?.created_at)}</dd></div>
                  <div><dt>Updated</dt><dd>{formatDate(detail?.updated_at)}</dd></div>
                </dl>
                <div className="row">
                  <Button size="sm" variant="secondary" icon="eye" onClick={() => setTab("workspace")}>Inspect workspace</Button>
                </div>
              </section>

              <section className="card">
                <div className="card-head">
                  <div><h2>Human actions</h2><p>Blocking actions the server is waiting on.</p></div>
                </div>
                {(detail?.human_actions || []).length === 0 ? (
                  <Empty title="Nothing waiting on a human" detail="Credential or decision requests from the server would appear here." />
                ) : (
                  <div className="action-list">
                    {detail?.human_actions.map((action) => (
                      <div key={action.id} className={`action-row ${action.status}`}>
                        <div>
                          <strong>{action.title}</strong>
                          <span>{action.description}</span>
                          {action.why && <small>{action.why}</small>}
                        </div>
                        <div className="row">
                          <Pill tone={action.status === "resolved" ? "green" : action.blocking ? "red" : "amber"}>{action.status}</Pill>
                          {action.status !== "resolved" && (
                            <Button size="sm" variant="secondary" icon="check" onClick={async () => {
                              setBusy("resolve");
                              try {
                                await api.resolveAction(projectId, action.id, "resolved", "Resolved from the output laptop.");
                                toast("Human action marked resolved.", "ok");
                                setDetail((await api.project(projectId)) as ProjectDetail);
                              } catch (err: unknown) {
                                toast((err as Error).message, "bad");
                              } finally {
                                setBusy("");
                              }
                            }} disabled={busy === "resolve"}>Resolve</Button>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              <section className="card">
                <div className="card-head">
                  <div><h2>Protocol gaps</h2><p>Surfaced instead of faked.</p></div>
                </div>
                <ul className="gap-list">
                  {snapshot.operator.protocol_gaps.map((gap) => <li key={gap}><Icon name="info" size={15} /><span>{gap}</span></li>)}
                </ul>
              </section>
            </div>
          )}

          {tab === "workspace" && (
            <div className="workspace">
              <div className="toolbar">
                <span className="mono small ellipsis">{path || "No workspace path"}</span>
                <Button size="sm" variant="secondary" icon="refresh" onClick={() => void loadWorkspace()} disabled={!path || loading === "workspace"}>
                  {loading === "workspace" ? "Reading…" : "Read status, diff and log"}
                </Button>
              </div>
              {!path ? (
                <Empty title="This project has no workspace path" detail="The server project row did not include workspace_path." />
              ) : (
                <div className="grid-2">
                  <section className="card">
                    <div className="card-head"><div><h2>Status</h2><p>GET /api/workspaces/status</p></div></div>
                    {status ? (
                      <dl className="kv">
                        <div><dt>Exists</dt><dd>{status.exists ? "yes" : "no"}</dd></div>
                        <div><dt>Git repo</dt><dd>{status.is_repo ? "yes" : "no"}</dd></div>
                        <div><dt>Branch</dt><dd className="mono">{status.branch || "—"}</dd></div>
                        <div><dt>HEAD</dt><dd className="mono">{shortSha(status.head)}</dd></div>
                        <div><dt>Changed</dt><dd>{status.changed ?? "—"}</dd></div>
                        <div><dt>Untracked</dt><dd>{status.untracked ?? "—"}</dd></div>
                        <div><dt>Ahead / behind</dt><dd>{status.ahead ?? "—"} / {status.behind ?? "—"}</dd></div>
                      </dl>
                    ) : (
                      <p className="muted small">Status has not been read yet.</p>
                    )}
                    {status?.error && <p className="bad small">{status.error}</p>}
                  </section>
                  <section className="card">
                    <div className="card-head"><div><h2>Recent commits</h2><p>GET /api/workspaces/log</p></div></div>
                    <pre className="code-block tall">{String((log as { log?: string })?.log || "No log read yet.")}</pre>
                  </section>
                  <section className="card span-2">
                    <div className="card-head"><div><h2>Diff</h2><p>GET /api/workspaces/diff — read-only</p></div></div>
                    {diff?.stat && <pre className="code-block">{String(diff.stat)}</pre>}
                    <pre className="code-block tall">{String(diff?.diff || "No diff read yet.")}</pre>
                  </section>
                </div>
              )}
            </div>
          )}

          {tab === "artifacts" && (
            <div className="artifacts">
              <div className="toolbar">
                <span className="small muted">Artifacts are server metadata; verification recomputes SHA-256 on this machine.</span>
                <Button size="sm" variant="secondary" icon="refresh" onClick={() => void loadArtifacts()}>Reload</Button>
                <Button size="sm" variant="secondary" icon="download" onClick={async () => {
                  try {
                    await api.collectArtifacts(projectId);
                    toast("Artifact collection requested.", "ok");
                    await loadArtifacts();
                  } catch (err: unknown) {
                    toast((err as Error).message, "bad");
                  }
                }}>Collect</Button>
              </div>
              {artifacts.length === 0 ? (
                <Empty title="No artifacts recorded" detail="The server has not registered delivery artifacts for this project, or collection has not run." />
              ) : (
                <div className="artifact-grid">
                  {artifacts.map((artifact) => (
                    <article key={artifact.id} className="artifact-card">
                      <div className="artifact-head">
                        <div>
                          <strong>{artifact.name}</strong>
                          <span className="mono small">{artifact.kind} · {artifact.id}</span>
                        </div>
                        <Pill tone={artifact.verified ? "green" : "amber"}>{artifact.verified ? "verified" : "unverified"}</Pill>
                      </div>
                      <dl className="kv tight">
                        <div><dt>Size</dt><dd>{formatBytes(artifact.size)}</dd></div>
                        <div><dt>SHA-256</dt><dd className="mono small">{shortSha(artifact.sha256)}</dd></div>
                        <div><dt>Task</dt><dd className="mono small">{artifact.task_id || "—"}</dd></div>
                        <div><dt>Created</dt><dd>{timeAgo(artifact.created_at)}</dd></div>
                      </dl>
                      <span className="mono small ellipsis artifact-path">{artifact.path}</span>
                      <div className="row">
                        <Button size="sm" variant="secondary" icon="hash" onClick={async () => {
                          try {
                            const result = (await api.verifyArtifact(projectId, artifact.id)) as { matches?: boolean; recomputed_sha256?: string };
                            toast(result.matches ? "SHA-256 matches the server record." : "Checksum mismatch — do not ship this artifact.", result.matches ? "ok" : "bad");
                            await loadArtifacts();
                          } catch (err: unknown) {
                            toast((err as Error).message, "bad");
                          }
                        }}>Verify checksum</Button>
                        <Button size="sm" variant="ghost" icon="copy" onClick={async () => {
                          try {
                            const result = (await api.copyArtifact(projectId, artifact.id)) as { copied_to?: string };
                            toast(result.copied_to ? `Copied to ${result.copied_to}` : "Artifact copied.", "ok");
                          } catch (err: unknown) {
                            toast((err as Error).message, "bad");
                          }
                        }}>Copy verified</Button>
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </div>
          )}

          {tab === "delivery" && (
            <div className="delivery">
              <div className="grid-2">
                <section className="card">
                  <div className="card-head"><div><h2>Verification command</h2><p>Runs on the control server inside the project workspace.</p></div></div>
                  <div className="field">
                    <label>Command</label>
                    <input value={command} onChange={(event) => setCommand(event.target.value)} placeholder="for example: npm test" spellCheck={false} />
                    <small>POST /api/delivery/run · explicit human action · up to 15 minutes.</small>
                  </div>
                  <div className="row">
                    <Button icon="play" disabled={!command.trim() || busy === "run"} onClick={async () => {
                      setBusy("run");
                      try {
                        const result = (await api.deliveryRun({ project_id: projectId, command: command.trim() })) as DeliveryRun;
                        toast(`Delivery command finished: ${result.status || "unknown"}.`, result.status === "succeeded" || result.status === "passed" ? "ok" : "warn");
                        await loadRuns();
                      } catch (err: unknown) {
                        toast((err as Error).message, "bad");
                      } finally {
                        setBusy("");
                      }
                    }}>{busy === "run" ? "Running…" : "Run verification"}</Button>
                    <Button variant="secondary" icon="logs" disabled={busy === "report"} onClick={async () => {
                      setBusy("report");
                      try {
                        await api.deliveryReport(projectId, "review");
                        toast("Server review report generated.", "ok");
                      } catch (err: unknown) {
                        toast((err as Error).message, "bad");
                      } finally {
                        setBusy("");
                      }
                    }}>{busy === "report" ? "Generating…" : "Generate review report"}</Button>
                  </div>
                </section>
                <section className="card">
                  <div className="card-head"><div><h2>Runs</h2><p>GET /api/delivery/runs</p></div></div>
                  {runs.length === 0 ? (
                    <p className="small muted">No delivery runs recorded for this project.</p>
                  ) : (
                    <div className="run-list">
                      {runs.map((run) => (
                        <div key={run.id} className="run-row">
                          <div>
                            <strong className="mono small">{run.command || run.id}</strong>
                            <span className="small muted">{run.created_at ? formatDate(run.created_at) : ""}</span>
                          </div>
                          <Pill tone={run.status === "succeeded" || run.status === "passed" ? "green" : run.status === "failed" ? "red" : "blue"}>
                            {run.status || "unknown"}
                          </Pill>
                          {run.output && <pre className="code-block small">{String(run.output).slice(-2000)}</pre>}
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              </div>
            </div>
          )}

          {tab === "documents" && (
            <div className="documents">
              <div className="toolbar">
                <span className="small muted">Server documents, read-only. Preview loads one document on demand.</span>
                <Button size="sm" variant="secondary" icon="refresh" onClick={() => void loadDocuments()}>Reload</Button>
              </div>
              <div className="grid-2">
                <section className="card">
                  <div className="card-head"><div><h2>Documents</h2><p>GET /api/documents</p></div></div>
                  {documents.length === 0 ? (
                    <p className="small muted">No documents on the server.</p>
                  ) : (
                    <div className="doc-list">
                      {documents.map((doc) => {
                        const id = String(doc.id || "");
                        return (
                          <button key={id} className="doc-row" onClick={async () => {
                            try {
                              const payload = (await api.document(id)) as { document?: Record<string, unknown>; content?: string; body?: string };
                              const record = payload.document || (payload as unknown as Record<string, unknown>);
                              setDocumentText({
                                id,
                                title: String(record.title || id),
                                body: String(payload.content || payload.body || record.content || record.body || JSON.stringify(payload, null, 2))
                              });
                            } catch (err: unknown) {
                              toast((err as Error).message, "bad");
                            }
                          }}>
                            <div>
                              <strong>{String(doc.title || doc.id)}</strong>
                              <span className="mono small">{String(doc.kind || doc.type || "document")} · {id}</span>
                            </div>
                            <Icon name="arrow" size={16} />
                          </button>
                        );
                      })}
                    </div>
                  )}
                </section>
                <section className="card">
                  <div className="card-head"><div><h2>{documentText?.title || "Preview"}</h2><p>Read-only text returned by the server.</p></div></div>
                  <pre className="code-block tall">{documentText?.body || "Choose a document to load it."}</pre>
                </section>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
