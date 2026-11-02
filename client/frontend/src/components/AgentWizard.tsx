import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../context";
import type { Agent, Job } from "../types";
import { Button, Icon, Modal, Pill, Progress, timeAgo } from "./ui";

type Operation = "install" | "update" | "verify";

const STAGE_LABELS: Record<string, string> = {
  starting: "Preparing",
  checking: "Checking existing binary",
  downloading: "Downloading official installer",
  verifying: "Verifying the official download",
  installing: "Running the installer",
  npm_fallback: "Official script failed; trying npm",
  configuring: "Configuring PATH",
  version_check: "Running --version",
  done: "Complete",
  failed: "Failed"
};

export function AgentWizard({
  kind,
  operation,
  onClose,
  onDone
}: {
  kind: string;
  operation: Operation;
  onClose: () => void;
  onDone: () => void;
}) {
  const { snapshot, toast, refresh, openAuth } = useApp();
  const agent = snapshot.agents.find((item) => item.kind === kind);
  const [job, setJob] = useState<Job | null>(null);
  const [log, setLog] = useState<Array<{ at: string; text: string }>>([]);
  const [offset, setOffset] = useState(0);
  const [started, setStarted] = useState(operation === "install");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [confirmUpdate, setConfirmUpdate] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);
  const timer = useRef<number | null>(null);

  const start = async (requested: Operation = operation) => {
    setBusy("start");
    setError("");
    try {
      let next: Job;
      if (requested === "install") next = (await api.installAgent(kind, false)) as Job;
      else if (requested === "update") next = (await api.updateAgent(kind)) as Job;
      else next = (await api.verifyAgent(kind)) as Job;
      setJob(next);
      setLog(next.log || []);
      setOffset(next.next_offset || 0);
      setStarted(true);
    } catch (err: unknown) {
      setError((err as Error).message);
    } finally {
      setBusy("");
    }
  };

  useEffect(() => {
    if (operation === "install") void start("install");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind]);

  useEffect(() => {
    if (!job || !["running"].includes(job.status)) return;
    const poll = async () => {
      try {
        const next = (await api.job(job.id, offset)) as Job;
        if (next.log?.length) setLog((rows) => [...rows, ...next.log].slice(-800));
        setOffset(next.next_offset || 0);
        setJob((previous) => (previous ? { ...previous, ...next } : next));
        if (next.status !== "running") {
          if (next.status === "succeeded") toast(`${kind} ${next.operation} completed.`, "ok");
          else toast(next.error || `${kind} ${next.operation} failed.`, "bad");
          await refresh();
        }
      } catch (err: unknown) {
        setError((err as Error).message);
      }
    };
    timer.current = window.setInterval(() => void poll(), 900);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [job?.id, job?.status, offset, kind, refresh, toast]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log]);

  const running = job?.status === "running";
  const finished = job && job.status !== "running";
  const version = (job?.result?.version as string) || agent?.version || "";

  return (
    <Modal
      wide
      title={`${operation === "update" ? "Update" : operation === "verify" ? "Verify" : "Install"} ${agent?.display_name || kind}`}
      subtitle={
        operation === "verify"
          ? "A real functional run. “Usable” is only set when the CLI answers with the verify token."
          : "Official HTTPS download, checksum/execution checks, then real --version output."
      }
      onClose={running ? () => toast("The job keeps running in the daemon; reopen it from Agents.", "warn") : onClose}
      footer={
        <div className="row spread">
          <span className="small muted">
            {job ? `${job.operation} · ${job.stage} · ${timeAgo(job.started_at)}` : "Not started"}
          </span>
          <div className="row">
            {finished && job.status === "succeeded" && operation !== "verify" && (
              <Button variant="secondary" icon="check" onClick={() => { setJob(null); setStarted(false); void start("verify"); }}>
                Verify usable
              </Button>
            )}
            {finished && job.status === "succeeded" && operation !== "verify" && (
              <Button variant="secondary" icon="terminal" onClick={() => { onClose(); openAuth({ kind }); }}>
                Sign in
              </Button>
            )}
            {!running && (
              <Button variant={finished ? "primary" : "secondary"} onClick={() => { onDone(); onClose(); }}>
                {finished ? "Done" : "Close"}
              </Button>
            )}
          </div>
        </div>
      }
    >
      {!started && operation === "update" && (
        <div className="wizard-confirm">
          <div className="inline-alert warn">
            <Icon name="alert" size={17} />
            <span>
              Updating runs the CLI's own updater. It can replace the binary at{" "}
              <span className="mono">{agent?.path || "the detected path"}</span> and briefly interrupt
              local work.
            </span>
          </div>
          <div className="stage-list">
            {(agent?.known_paths || []).map((path) => (
              <div key={path} className="stage-row">
                <span className="stage-index">·</span>
                <span className="mono">{path}</span>
              </div>
            ))}
          </div>
          <label className="check">
            <input type="checkbox" checked={confirmUpdate} onChange={(event) => setConfirmUpdate(event.target.checked)} />
            <span>I approve running <span className="mono">{kind === "qoder" ? "qodercli update" : "opencode upgrade"}</span> now.</span>
          </label>
          <Button icon="download" disabled={!confirmUpdate || busy === "start"} onClick={() => void start("update")}>
            Run update
          </Button>
        </div>
      )}

      {!started && operation === "verify" && (
        <div className="wizard-confirm">
          <p>
            Verification sends a short deterministic prompt to the CLI and looks for the token{" "}
            <span className="mono">FMCM_VERIFY_OK</span>. It is a real run and may consume the agent's
            quota.
          </p>
          <Button icon="check" onClick={() => void start("verify")} disabled={busy === "start"}>
            Run functional verification
          </Button>
        </div>
      )}

      {(started || job) && (
        <>
          <div className="job-head">
            <div className="job-head-copy">
              <strong>{STAGE_LABELS[job?.stage || "starting"] || job?.stage || "Starting"}</strong>
              <span>{job?.message || "Waiting for the installer to report a real stage…"}</span>
            </div>
            <Pill tone={job?.status === "succeeded" ? "green" : job?.status === "failed" ? "red" : "blue"}>
              {job?.status || "starting"}
            </Pill>
          </div>
          <Progress
            value={job?.status === "succeeded" ? 100 : job?.status === "failed" ? 100 : stagePercent(job?.stage)}
            tone={job?.status === "failed" ? "red" : job?.status === "succeeded" ? "green" : "blue"}
          />

          <div className="stage-track">
            {["checking", "downloading", "installing", "version_check", "done"].map((stage) => (
              <span key={stage} className={stageState(stage, job?.stage)}>
                {STAGE_LABELS[stage]}
              </span>
            ))}
          </div>

          <div className="log-view" ref={logRef}>
            {log.length === 0 && <span className="muted">No log lines yet.</span>}
            {log.map((line, index) => (
              <div key={`${line.at}-${index}`}>
                <time>{new Date(line.at).toLocaleTimeString()}</time>
                <span>{line.text}</span>
              </div>
            ))}
          </div>

          {finished && job.result && (
            <div className={`job-result ${job.status}`}>
              <strong>{job.status === "succeeded" ? "Finished" : "Failed"}</strong>
              <dl className="kv">
                {version && <div><dt>Version</dt><dd className="mono">{version}</dd></div>}
                {(job.result.path as string) && <div><dt>Path</dt><dd className="mono ellipsis">{String(job.result.path)}</dd></div>}
                {(job.result.source as string) && <div><dt>Install route</dt><dd>{String(job.result.source)}</dd></div>}
                {job.result.duration_ms != null && <div><dt>Duration</dt><dd>{Math.round(Number(job.result.duration_ms) / 1000)}s</dd></div>}
              </dl>
              {job.error && <p className="bad">{job.error}</p>}
            </div>
          )}
        </>
      )}

      {error && <div className="pair-error"><Icon name="alert" size={17} /><div><strong>Request failed</strong><span>{error}</span></div></div>}
    </Modal>
  );
}

function stagePercent(stage?: string): number {
  switch (stage) {
    case "checking": return 12;
    case "downloading": return 34;
    case "verifying": return 48;
    case "installing": return 62;
    case "npm_fallback": return 70;
    case "configuring": return 80;
    case "version_check": return 92;
    case "done": return 100;
    default: return 6;
  }
}

function stageState(stage: string, current?: string): string {
  const order = ["checking", "downloading", "installing", "version_check", "done"];
  const index = order.indexOf(stage);
  const currentIndex = order.indexOf(current || "");
  if (current === "failed") return index === 0 ? "failed" : "";
  if (currentIndex === -1) return "";
  if (index < currentIndex) return "done";
  if (index === currentIndex) return "active";
  return "";
}

export type { Agent };
