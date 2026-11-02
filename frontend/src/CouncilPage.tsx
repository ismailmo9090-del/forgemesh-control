import { useCallback, useEffect, useMemo, useState } from "react";
import { api, formatTime } from "./api";
import { Card, Empty, Modal, PROVIDER_META, StatusChip } from "./components/ui";
import type {
  CouncilBrain,
  CouncilDisagreement,
  CouncilMeeting,
  CouncilMeetingSummary,
  CouncilProblem,
  CouncilRoster,
  CouncilSummary,
  Project
} from "./types";

interface DecisionContract {
  ok: boolean;
  missing: string[];
  warnings?: string[];
  unvalidated?: boolean;
  selected_proposal?: string | null;
  selected_title?: string | null;
  selected_account?: string | null;
  [key: string]: unknown;
}

export function CouncilPage({ projects }: { projects: Project[] }) {
  const [roster, setRoster] = useState<CouncilRoster | null>(null);
  const [summary, setSummary] = useState<CouncilSummary | null>(null);
  const [meetings, setMeetings] = useState<CouncilMeetingSummary[]>([]);
  const [problems, setProblems] = useState<CouncilProblem[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [openId, setOpenId] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    const results = await Promise.allSettled([
      api<CouncilRoster>("/api/council/providers"),
      api<{ meetings: CouncilMeetingSummary[] }>("/api/council/meetings?limit=50"),
      api<CouncilSummary>("/api/council/summary"),
      api<{ problems: CouncilProblem[] }>("/api/problems")
    ]);
    if (results[0].status === "fulfilled") setRoster(results[0].value);
    if (results[1].status === "fulfilled") setMeetings(results[1].value.meetings);
    if (results[2].status === "fulfilled") setSummary(results[2].value);
    if (results[3].status === "fulfilled") setProblems(results[3].value.problems);
    const failure = results.find((result) => result.status === "rejected");
    if (failure && failure.status === "rejected") {
      setError(failure.reason instanceof Error ? failure.reason.message : String(failure.reason));
    } else {
      setError("");
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function createMeeting(form: {
    agenda: string;
    mode: string;
    projectId: string;
    problemId: string;
    providers: string[];
    rounds: number;
  }) {
    setBusy("create");
    setError("");
    try {
      const meeting = await api<CouncilMeeting>("/api/council/meetings", {
        method: "POST",
        body: JSON.stringify({
          agenda: form.agenda,
          mode: form.mode,
          project_id: form.projectId || null,
          problem_id: form.problemId || null,
          providers: form.providers,
          rounds: form.rounds
        })
      });
      await load();
      setOpenId(meeting.id);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy("");
    }
  }

  async function createProblem(form: { title: string; description: string }): Promise<boolean> {
    setBusy("problem");
    setError("");
    try {
      await api("/api/problems", {
        method: "POST",
        body: JSON.stringify(form)
      });
      await load();
      return true;
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
      return false;
    } finally {
      setBusy("");
    }
  }

  const availableCount = roster?.providers.filter((item) => item.status === "available").length || 0;

  return (
    <div className="page-stack">
      <header className="page-head">
        <div>
          <h1>AI Council</h1>
          <p>
            Each seat is a real, signed-in ForgeMesh Browser session. Proposals, disagreements and
            the synthesis are stored verbatim; a majority vote is never substituted for them.
          </p>
        </div>
        <button className="btn ghost" onClick={() => void load()} disabled={loading}>
          {loading ? "Reading…" : "Refresh roster"}
        </button>
      </header>

      {error && <p className="error-box">{error}</p>}

      <div className="council-top">
        <Card
          title="Provider roster"
          subtitle={
            roster
              ? `${availableCount}/${roster.providers.length} providers available · checked ${formatTime(roster.checked_at)}`
              : "Reading the browser runtime…"
          }
          live
        >
          {roster && !roster.connected && (
            <p className="warn-box">
              ForgeMesh Browser is unreachable{roster.error ? `: ${roster.error}` : ""}. Council seats
              cannot be verified until it answers.
            </p>
          )}
          <div className="roster-grid">
            {(roster?.providers || []).map((provider) => {
              const meta = PROVIDER_META[provider.provider_id] || {
                label: provider.display_name,
                glyph: "•",
                color: "var(--accent)"
              };
              return (
                <div key={provider.provider_id} className={`roster-card ${provider.status}`}>
                  <header>
                    <span className="roster-glyph" style={{ color: meta.color }}>
                      {meta.glyph}
                    </span>
                    <div>
                      <strong>{provider.display_name}</strong>
                      <small>{provider.reason || `${provider.seats.length} seat(s)`}</small>
                    </div>
                    <StatusChip status={provider.status} />
                  </header>
                  {provider.seats.length > 0 && (
                    <ul className="seat-list">
                      {provider.seats.map((seat) => (
                        <li key={`${provider.provider_id}-${seat.profile_id}-${seat.account_id}`}>
                          <div>
                            <strong>{seat.label || seat.profile_id}</strong>
                            <small>
                              {seat.profile_id}
                              {seat.council_role ? ` · ${seat.council_role}` : ""}
                            </small>
                          </div>
                          <StatusChip status={seat.status} />
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              );
            })}
          </div>
        </Card>

        <div className="council-side">
          <Card
            title="Council activity"
            subtitle={
              summary
                ? `${summary.running} running · ${Object.entries(summary.by_status)
                    .map(([status, count]) => `${count} ${status}`)
                    .join(", ") || "no meetings yet"}`
                : "Reading…"
            }
          >
            {summary?.last ? (
              <div className="detail-grid">
                <div>
                  <span>Last meeting</span>
                  <strong className="ellipsis">{summary.last.agenda}</strong>
                </div>
                <div>
                  <span>Status</span>
                  <StatusChip status={summary.last.status} />
                </div>
                <div>
                  <span>Rounds</span>
                  <strong>
                    {summary.last.current_round}/{summary.last.rounds_total}
                  </strong>
                </div>
                <div>
                  <span>Created</span>
                  <strong>{formatTime(summary.last.created_at)}</strong>
                </div>
              </div>
            ) : (
              <Empty
                title="No Council meeting yet"
                detail="Create one below. Nothing runs until you press Start."
              />
            )}
          </Card>
          <ProblemCard busy={busy === "problem"} onCreate={createProblem} />
        </div>
      </div>

      <CreateMeetingCard
        roster={roster}
        projects={projects}
        problems={problems}
        busy={busy === "create"}
        onCreate={createMeeting}
      />

      <Card title="Meetings" subtitle={`${meetings.length} stored meeting(s)`}>
        {meetings.length === 0 ? (
          <Empty
            title="No meetings"
            detail="A meeting is a stored record of real prompts and responses. None exist yet."
          />
        ) : (
          <div className="meeting-list">
            {meetings.map((meeting) => (
              <button key={meeting.id} className="meeting-row" onClick={() => setOpenId(meeting.id)}>
                <div className="meeting-main">
                  <strong>{meeting.agenda}</strong>
                  <small className="muted">
                    {meeting.mode} · {meeting.id} · created {formatTime(meeting.created_at)}
                  </small>
                </div>
                <span className="mono small">
                  {meeting.current_round}/{meeting.rounds_total}
                </span>
                <StatusChip status={meeting.status} />
              </button>
            ))}
          </div>
        )}
      </Card>

      {openId && (
        <MeetingDetail
          meetingId={openId}
          projects={projects}
          onClose={() => setOpenId("")}
          onChanged={() => void load()}
        />
      )}
    </div>
  );
}

function ProblemCard({
  busy,
  onCreate
}: {
  busy: boolean;
  onCreate: (form: { title: string; description: string }) => Promise<boolean>;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");

  return (
    <Card
      title="Problem statement"
      subtitle="Optional structured brief for Hackathon mode"
      action={
        <button className="link-btn" onClick={() => setOpen((value) => !value)}>
          {open ? "Close" : "Add →"}
        </button>
      }
    >
      {open ? (
        <div className="stack">
          <label className="field">
            <span>Title</span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <label className="field">
            <span>Description</span>
            <textarea
              rows={3}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          <button
            className="btn primary"
            disabled={!title.trim() || busy}
            onClick={async () => {
              const saved = await onCreate({ title: title.trim(), description });
              if (!saved) return;
              setTitle("");
              setDescription("");
              setOpen(false);
            }}
          >
            {busy ? "Saving…" : "Save problem"}
          </button>
        </div>
      ) : (
        <p className="muted small">
          Problem statements are optional. In Hackathon mode the Council can work from an uploaded
          document instead.
        </p>
      )}
    </Card>
  );
}

function CreateMeetingCard({
  roster,
  projects,
  problems,
  busy,
  onCreate
}: {
  roster: CouncilRoster | null;
  projects: Project[];
  problems: CouncilProblem[];
  busy: boolean;
  onCreate: (form: {
    agenda: string;
    mode: string;
    projectId: string;
    problemId: string;
    providers: string[];
    rounds: number;
  }) => Promise<void>;
}) {
  const usable = useMemo(
    () => (roster?.providers || []).filter((provider) => provider.status === "available"),
    [roster]
  );
  const [agenda, setAgenda] = useState("");
  const [mode, setMode] = useState("normal");
  const [projectId, setProjectId] = useState(projects[0]?.id || "");
  const [problemId, setProblemId] = useState("");
  const [rounds, setRounds] = useState(3);
  const [selected, setSelected] = useState<string[]>([]);

  useEffect(() => {
    setSelected((previous) => {
      const stillUsable = previous.filter((id) => usable.some((item) => item.provider_id === id));
      if (stillUsable.length > 0) return stillUsable;
      return usable.map((item) => item.provider_id);
    });
  }, [usable]);

  useEffect(() => {
    if (!projectId && projects[0]) setProjectId(projects[0].id);
  }, [projects, projectId]);

  return (
    <Card title="Create meeting" subtitle="Drafts are stored immediately; nothing is sent until Start">
      <div className="form-grid">
        <label className="field wide">
          <span>Agenda</span>
          <textarea
            rows={3}
            value={agenda}
            onChange={(event) => setAgenda(event.target.value)}
            placeholder="What must the Council decide?"
          />
        </label>
        <label className="field">
          <span>Mode</span>
          <select value={mode} onChange={(event) => setMode(event.target.value)}>
            <option value="normal">Normal project</option>
            <option value="hackathon">Hackathon</option>
          </select>
        </label>
        <label className="field">
          <span>Rounds (2–10)</span>
          <input
            type="number"
            min={2}
            max={10}
            value={rounds}
            onChange={(event) =>
              setRounds(Math.max(2, Math.min(10, Number(event.target.value) || 2)))
            }
          />
        </label>
        <label className="field">
          <span>Project</span>
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">No project</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name} ({project.mode})
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Problem statement</span>
          <select value={problemId} onChange={(event) => setProblemId(event.target.value)}>
            <option value="">None</option>
            {problems.map((problem) => (
              <option key={problem.id} value={problem.id}>
                {problem.title}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="seat-picker">
        <span className="muted small">Seats</span>
        <div className="seat-options">
          {(roster?.providers || []).map((provider) => {
            const disabled = provider.status !== "available";
            const checked = selected.includes(provider.provider_id);
            return (
              <label
                key={provider.provider_id}
                className={`seat-option ${disabled ? "disabled" : ""}`}
                title={provider.reason || provider.account_label}
              >
                <input
                  type="checkbox"
                  disabled={disabled}
                  checked={checked}
                  onChange={(event) =>
                    setSelected((previous) =>
                      event.target.checked
                        ? [...previous, provider.provider_id]
                        : previous.filter((id) => id !== provider.provider_id)
                    )
                  }
                />
                <span>{provider.display_name}</span>
                {disabled && <small className="muted">{provider.status}</small>}
              </label>
            );
          })}
        </div>
        {roster && usable.length === 0 && (
          <p className="warn-box">
            No provider account reports <code>available</code>. Register a signed-in profile in
            ForgeMesh Browser first; a meeting without seats cannot run.
          </p>
        )}
      </div>

      <div className="row spread">
        <p className="muted small">
          Starting a meeting performs real browser automation on the selected accounts. Only start it
          when that is acceptable.
        </p>
        <button
          className="btn primary"
          disabled={!agenda.trim() || selected.length === 0 || busy}
          onClick={() =>
            void onCreate({
              agenda: agenda.trim(),
              mode,
              projectId,
              problemId,
              providers: selected,
              rounds
            })
          }
        >
          {busy ? "Creating…" : "Create draft"}
        </button>
      </div>
    </Card>
  );
}

function brainLabel(kind: string | null | undefined): string {
  if (kind === "qoder") return "Qoder";
  if (kind === "opencode") return "OpenCode";
  return "control brain";
}

function brainTakeover(kind: string | null | undefined): string {
  return kind === "opencode" ? " (OpenCode took over after Qoder failed)" : "";
}

function describeDisagreement(item: CouncilDisagreement | string): string {
  if (typeof item === "string") return item;
  const severity = item.severity ? `[${item.severity}] ` : "";
  const author = item.raised_by ? `${item.raised_by} ` : "";
  const target = item.proposal ? `against ${item.proposal}: ` : "";
  const claim = item.counter_claim || item.claim || "";
  return `${severity}${author}${target}${claim}`.trim() || "Disagreement recorded without detail.";
}

function MeetingDetail({
  meetingId,
  projects,
  onClose,
  onChanged
}: {
  meetingId: string;
  projects: Project[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const [meeting, setMeeting] = useState<CouncilMeeting | null>(null);
  const [contract, setContract] = useState<DecisionContract | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [instruction, setInstruction] = useState("");
  const [projectId, setProjectId] = useState("");
  const [blueprint, setBlueprint] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(async () => {
    try {
      const detail = await api<CouncilMeeting>(`/api/council/meetings/${meetingId}`);
      setMeeting(detail);
      setProjectId((current) => current || detail.project_id || projects[0]?.id || "");
      if (detail.decision) {
        const decision = await api<{ contract: DecisionContract }>(
          `/api/council/meetings/${meetingId}/decision`
        );
        setContract(decision.contract);
      } else {
        setContract(null);
      }
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    }
  }, [meetingId, projects]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!meeting?.running) return;
    const timer = window.setInterval(() => void load(), 3500);
    return () => window.clearInterval(timer);
  }, [meeting?.running, load]);

  async function act(
    path: string,
    body: Record<string, unknown>,
    label: string
  ): Promise<boolean> {
    setBusy(label);
    setError("");
    try {
      await api(path, { method: "POST", body: JSON.stringify(body) });
      await load();
      onChanged();
      return true;
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
      return false;
    } finally {
      setBusy("");
    }
  }

  async function makeBlueprint() {
    if (!projectId) {
      setError("Choose a project before saving the blueprint.");
      return;
    }
    setBusy("blueprint");
    setError("");
    try {
      const result = await api<{ blueprint: Record<string, unknown>; approval: Record<string, unknown> }>(
        `/api/council/meetings/${meetingId}/blueprint?project_id=${encodeURIComponent(projectId)}`,
        { method: "POST" }
      );
      setBlueprint(result);
      onChanged();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy("");
    }
  }

  const scores = (meeting?.decision?.scores || {}) as Record<string, unknown>;
  const ranking = Array.isArray(scores.ranking) ? (scores.ranking as Array<Record<string, unknown>>) : [];
  const unresolved = Array.isArray(scores.unresolved) ? (scores.unresolved as unknown[]) : [];
  // A decision stores disagreement objects (proposal, counter_claim, severity,
  // raised_by ...). Rendering one directly as a React child blanked the page
  // with React error #31; normalise the list before mapping it.
  const disagreements = Array.isArray(meeting?.decision?.disagreements)
    ? meeting.decision.disagreements
    : [];
  const participants = Array.isArray(meeting?.participants) ? meeting.participants : [];
  const messages = Array.isArray(meeting?.messages) ? meeting.messages : [];
  const proposals = Array.isArray(meeting?.proposals) ? meeting.proposals : [];
  const rawBrain =
    meeting && typeof meeting.context === "object" && meeting.context
      ? (meeting.context as Record<string, unknown>).brain
      : null;
  const brain =
    rawBrain && typeof rawBrain === "object" ? (rawBrain as CouncilBrain) : null;

  return (
    <Modal title={meeting ? `Meeting · ${meeting.agenda}` : "Meeting"} onClose={onClose} width="1020px">
      {!meeting ? (
        <p className="muted">{error || "Reading meeting…"}</p>
      ) : (
        <div className="stack">
          {error && <p className="error-box">{error}</p>}

          <div className="detail-grid">
            <div>
              <span>Status</span>
              <StatusChip status={meeting.status} />
            </div>
            <div>
              <span>Mode</span>
              <strong>{meeting.mode}</strong>
            </div>
            <div>
              <span>Rounds</span>
              <strong>
                {meeting.current_round}/{meeting.rounds_total}
              </strong>
            </div>
            <div>
              <span>Started</span>
              <strong>{meeting.started_at ? formatTime(meeting.started_at) : "not started"}</strong>
            </div>
            <div>
              <span>Ended</span>
              <strong>{meeting.ended_at ? formatTime(meeting.ended_at) : "—"}</strong>
            </div>
            <div>
              <span>Project</span>
              <strong className="mono small">{meeting.project_id || "—"}</strong>
            </div>
          </div>

          <div className="detail-actions">
            <button
              className="btn primary"
              disabled={!!busy || meeting.running || meeting.status === "completed"}
              onClick={() => void act(`/api/council/meetings/${meetingId}/start`, {}, "start")}
            >
              {busy === "start" ? "Starting…" : "Start meeting"}
            </button>
            <button
              className="btn ghost"
              disabled={!!busy || !meeting.running}
              onClick={() =>
                void act(`/api/council/meetings/${meetingId}/stop`, {
                  reason: "cancelled from the dashboard"
                }, "stop")
              }
            >
              {busy === "stop" ? "Stopping…" : "Stop meeting"}
            </button>
            <button className="btn ghost" onClick={() => void load()} disabled={!!busy}>
              Reload
            </button>
          </div>

          <section>
            <h3>Instructions</h3>
            {meeting.instructions && <pre className="text-box">{meeting.instructions}</pre>}
            <div className="row">
              <input
                value={instruction}
                onChange={(event) => setInstruction(event.target.value)}
                placeholder="Add an operator instruction the Council must respect"
              />
              <button
                className="btn ghost"
                disabled={!instruction.trim() || !!busy}
                onClick={async () => {
                  const sent = await act(`/api/council/meetings/${meetingId}/instructions`, {
                    text: instruction.trim()
                  }, "instruction");
                  if (sent) setInstruction("");
                }}
              >
                Send
              </button>
            </div>
          </section>

          <section>
            <h3>Control brain</h3>
            <p className="muted small">
              Order: Qoder first, OpenCode only when Qoder fails. The brain writes the shared
              brief and the final polish; ForgeMesh itself performs the browser automation.
            </p>
            {brain?.brief ? (
              <div className="decision-box">
                <strong className="small">
                  Meeting brief · {brainLabel(brain.brief_by)}
                  {brainTakeover(brain.brief_by)}
                </strong>
                <pre className="text-box">{brain.brief}</pre>
              </div>
            ) : (
              <p className="muted small">
                {brain?.brief_error
                  ? `No brief was written: ${brain.brief_error}`
                  : "No brain brief is recorded for this meeting."}
              </p>
            )}
            {brain?.brief_attempts && brain.brief_attempts.length > 0 && (
              <ul className="statement-list">
                {brain.brief_attempts.map((attempt, index) => (
                  <li key={index}>
                    {brainLabel(attempt.kind)}: {attempt.ok ? "answered" : attempt.detail || "failed"}
                    {attempt.model ? ` · ${attempt.model}` : ""}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section>
            <h3>Participants</h3>
            {participants.length === 0 ? (
              <p className="muted small">No participant has been seated yet.</p>
            ) : (
              <div className="participant-table">
                {participants.map((participant) => (
                  <div key={`${participant.provider_id}-${participant.profile_id}`} className="participant-row">
                    <strong>{participant.provider_id}</strong>
                    <span className="muted small">
                      {participant.account_label || participant.profile_id}
                      {participant.council_role ? ` · ${participant.council_role}` : ""}
                    </span>
                    <StatusChip
                      status={participant.included ? participant.status : "offline"}
                      label={participant.included ? participant.status : "excluded"}
                    />
                    {participant.last_error && (
                      <span className="error-text small">{participant.last_error}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>

          <section>
            <h3>Rounds &amp; messages ({messages.length})</h3>
            {messages.length === 0 ? (
              <p className="muted small">
                No prompt has been sent yet. Messages appear here only after a real response.
              </p>
            ) : (
              <div className="message-list">
                {messages.map((message) => (
                  <div key={message.id} className={`message-row ${message.error ? "failed" : ""}`}>
                    <header>
                      <strong>
                        R{message.round} · {message.provider_id}
                      </strong>
                      <span className="muted small">
                        {message.kind} · {formatTime(message.created_at)}
                      </span>
                    </header>
                    {message.content && <pre className="text-box">{message.content}</pre>}
                    {message.error && <p className="error-box">{message.error}</p>}
                  </div>
                ))}
              </div>
            )}
          </section>

          <section>
            <h3>Proposals ({proposals.length})</h3>
            {proposals.length === 0 ? (
              <p className="muted small">No structured proposal has been recorded.</p>
            ) : (
              <div className="proposal-list">
                {proposals.map((proposal) => {
                  const recorded = Array.isArray(proposal.responses) ? proposal.responses : [];
                  const steps = recorded.flatMap((response) =>
                    Array.isArray(response.steps) ? (response.steps as unknown[]) : []
                  );
                  const acceptance = recorded.flatMap((response) =>
                    Array.isArray(response.acceptance) ? (response.acceptance as unknown[]) : []
                  );
                  return (
                    <article key={proposal.id} className={`proposal-card ${proposal.status}`}>
                      <header>
                        <div>
                          <strong>{proposal.title}</strong>
                          <small className="muted">
                            {proposal.author} · {proposal.account_label || proposal.id}
                          </small>
                        </div>
                        <StatusChip status={proposal.status} />
                      </header>
                      <p>{proposal.description}</p>
                      <div className="proposal-meta">
                        <span>Feasibility {proposal.feasibility ?? "—"}</span>
                        <span>Confidence {proposal.confidence ?? "—"}</span>
                      </div>
                      {steps.length > 0 && (
                        <ol className="statement-list">
                          {steps.slice(0, 12).map((step, index) => (
                            <li key={index}>{String(step)}</li>
                          ))}
                        </ol>
                      )}
                      {acceptance.length > 0 && (
                        <div>
                          <strong className="small">Acceptance</strong>
                          <ul className="statement-list">
                            {acceptance.slice(0, 12).map((item, index) => (
                              <li key={index}>{String(item)}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {Array.isArray(proposal.risks) && proposal.risks.length > 0 && (
                        <p className="warn-text small">Risks: {proposal.risks.join("; ")}</p>
                      )}
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          <section>
            <h3>Decision</h3>
            {!meeting.decision ? (
              <p className="muted small">
                The Council has not produced a stored decision. No selection is inferred.
              </p>
            ) : (
              <div className="decision-box">
                <p>{meeting.decision.summary}</p>
                <p className="muted">{meeting.decision.rationale}</p>
                {ranking.length > 0 && (
                  <div>
                    <strong className="small">Ranking (no simple majority)</strong>
                    <ol className="statement-list">
                      {ranking.map((item, index) => (
                        <li key={index}>
                          {String(item.title || item.proposal_id || "proposal")}
                          {item.author ? ` · ${String(item.author)}` : ""}
                          {typeof item.final === "number" ? ` · final ${item.final}` : " · unscored"}
                          {Array.isArray(item.objections) && item.objections.length > 0
                            ? ` · ${item.objections.length} objection(s)`
                            : ""}
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
                {disagreements.length > 0 && (
                  <div>
                    <strong className="small">Disagreements preserved</strong>
                    <ul className="statement-list">
                      {disagreements.map((item, index) => (
                        <li key={index}>{describeDisagreement(item)}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {unresolved.length > 0 && (
                  <div>
                    <strong className="small">Unresolved</strong>
                    <ul className="statement-list">
                      {unresolved.map((item, index) => (
                        <li key={index}>{String(item)}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {contract && (
                  <div className="stack">
                    <p className={contract.ok && !contract.unvalidated ? "info-box" : "warn-box"}>
                      Execution contract:{" "}
                      {contract.ok
                        ? contract.unvalidated
                          ? "structurally complete but unvalidated — the selected proposal has no peer critique, so this is not an approval."
                          : "complete — every required element is present."
                        : `blocked — ${contract.missing.join(", ")}`}
                    </p>
                    {contract.selected_title && (
                      <p className="muted small">
                        Selected: <strong>{contract.selected_title}</strong>
                        {contract.selected_account ? ` · ${contract.selected_account}` : ""}
                      </p>
                    )}
                    {contract.warnings?.length ? (
                      <ul className="statement-list">
                        {contract.warnings.map((warning, index) => (
                          <li key={index}>{warning}</li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                )}
              </div>
            )}
          </section>

          {meeting.decision && (
            <section>
              <h3>Final answer (polished)</h3>
              {brain?.polished ? (
                <div className="decision-box">
                  <p className="muted small">
                    Written by {brainLabel(brain.polished_by)}
                    {brainTakeover(brain.polished_by)} from the stored decision. This is an
                    explanation, not a second decision.
                  </p>
                  <pre className="text-box">{brain.polished}</pre>
                  {brain.polish_attempts && brain.polish_attempts.length > 0 && (
                    <ul className="statement-list">
                      {brain.polish_attempts.map((attempt, index) => (
                        <li key={index}>
                          {brainLabel(attempt.kind)}:{" "}
                          {attempt.ok ? "answered" : attempt.detail || "failed"}
                          {attempt.model ? ` · ${attempt.model}` : ""}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : (
                <p className="muted small">
                  {brain?.polish_error
                    ? `No polished answer: ${brain.polish_error}`
                    : "No polished answer was recorded for this meeting."}
                </p>
              )}
            </section>
          )}

          {meeting.decision && (
            <section>
              <h3>Blueprint &amp; approval</h3>
              <div className="row">
                <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
                  <option value="">Select project…</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
                <button className="btn" disabled={!projectId || !!busy} onClick={() => void makeBlueprint()}>
                  {busy === "blueprint" ? "Saving…" : "Save blueprint & request approval"}
                </button>
              </div>
              {blueprint && (
                <p className="info-box">
                  Blueprint saved and approval requested. The implementation gate stays blocked until
                  a human approves it on the Projects page.
                </p>
              )}
              {!blueprint && (
                <p className="muted small">
                  {projects.length === 0
                    ? "Create a project first: a blueprint has to be saved into one."
                    : "This decision is not a blueprint yet. Pick the project that should receive it, then press Save blueprint & request approval."}
                </p>
              )}
              {blueprint && (
                <pre className="json-box">{JSON.stringify(blueprint, null, 2)}</pre>
              )}
            </section>
          )}
        </div>
      )}
    </Modal>
  );
}
