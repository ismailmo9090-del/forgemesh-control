import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../context";
import type { AuthSessionView } from "../types";
import { Button, Icon, Modal, Pill, timeAgo } from "./ui";

const ANSI = /\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r/g;

function cleanTerminal(text: string): string {
  return text.replace(ANSI, "");
}

export function AuthTerminal({
  kind,
  sessionId,
  onClose,
  onDone
}: {
  kind: string;
  sessionId?: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const { snapshot, toast, refresh, openAuth } = useApp();
  const agent = snapshot.agents.find((item) => item.kind === kind);
  const [id, setId] = useState(sessionId || "");
  const [view, setView] = useState<AuthSessionView | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [started, setStarted] = useState(Boolean(sessionId));
  const outputRef = useRef<HTMLDivElement | null>(null);
  const timer = useRef<number | null>(null);

  const live = view?.status === "running";
  const finished = view && view.status !== "running";
  const history = useMemo(
    () => snapshot.auth_history.filter((row) => row.kind === kind),
    [snapshot.auth_history, kind]
  );

  const start = async () => {
    setBusy("start");
    setError("");
    try {
      const next = (await api.startLogin(kind)) as AuthSessionView;
      setId(next.id);
      setView(next);
      setOffset(next.next_offset || 0);
      setStarted(true);
    } catch (err: unknown) {
      setError((err as Error).message);
    } finally {
      setBusy("");
    }
  };

  const openHistory = async (historyId: string) => {
    setError("");
    setBusy("history");
    try {
      const next = (await api.authSession(kind, historyId, 0)) as AuthSessionView;
      setId(historyId);
      setView(next);
      setOffset(next.next_offset || 0);
      setStarted(true);
    } catch (err: unknown) {
      setError((err as Error).message);
    } finally {
      setBusy("");
    }
  };

  useEffect(() => {
    if (!id) return;
    const poll = async () => {
      try {
        const next = (await api.authSession(kind, id, offset)) as AuthSessionView;
        if (next.text) {
          setView((previous) =>
            previous
              ? {
                  ...previous,
                  ...next,
                  text: next.finished ? next.text : (previous.text || "") + next.text
                }
              : next
          );
        } else {
          setView((previous) => (previous ? { ...previous, ...next, text: previous.text } : next));
        }
        setOffset(next.next_offset || 0);
        if (next.status !== "running" && !next.finished) {
          if (timer.current) window.clearInterval(timer.current);
          await refresh();
        }
      } catch (err: unknown) {
        setError((err as Error).message);
      }
    };
    timer.current = window.setInterval(() => void poll(), 650);
    void poll();
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
    // The offset moves with every response; recreating the timer is intentional.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, kind, offset]);

  useEffect(() => {
    if (outputRef.current) outputRef.current.scrollTop = outputRef.current.scrollHeight;
  }, [view?.text]);

  const send = async (payload: { text?: string; key?: string }) => {
    if (!id) return;
    setBusy(payload.key || "text");
    try {
      const next = (payload.key
        ? await api.authKey(kind, id, payload.key)
        : await api.authInput(kind, id, payload.text || "")) as AuthSessionView;
      if (next.text) {
        setView((previous) => (previous ? { ...previous, text: (previous.text || "") + next.text } : next));
      }
      setOffset(next.next_offset || 0);
      if (payload.text) setText("");
    } catch (err: unknown) {
      toast((err as Error).message, "bad");
    } finally {
      setBusy("");
    }
  };

  const cancel = async () => {
    if (!id) return;
    setBusy("cancel");
    try {
      await api.authCancel(kind, id);
      toast("Sign-in session cancelled.", "warn");
      await refresh();
    } catch (err: unknown) {
      toast((err as Error).message, "bad");
    } finally {
      setBusy("");
    }
  };

  const outcomeTone = view?.outcome === "authenticated" ? "green" : finished ? "amber" : "blue";

  return (
    <Modal
      wide
      title={`${agent?.display_name || kind} sign-in`}
      subtitle="A real PTY hosted by the daemon. The app never types credentials for you."
      onClose={onClose}
      footer={
        <div className="row spread">
          <span className="small muted">
            {view ? `${view.command || "login"} · ${view.started_at ? timeAgo(view.started_at) : "now"}` : "Not started"}
          </span>
          <div className="row">
            {live && (
              <Button variant="danger" icon="stop" onClick={cancel} disabled={busy === "cancel"}>
                Cancel session
              </Button>
            )}
            <Button variant="secondary" onClick={onClose}>Close</Button>
          </div>
        </div>
      }
    >
      {!started && (
        <div className="auth-intro">
          <div className="auth-hero">
            <Icon name="terminal" size={26} />
            <div>
              <h3>Host the CLI's own login</h3>
              <p>
                {agent?.display_name || kind} runs in a pseudo-terminal inside the local bridge. Output
                is streamed verbatim; the only bytes sent to it are the ones a human types below.
              </p>
            </div>
          </div>
          <ul className="checklist">
            <li><Icon name="check" size={15} /> CAPTCHA, OTP and MFA prompts pause the flow and are never auto-filled.</li>
            <li><Icon name="check" size={15} /> Success is only reported after the CLI's own status command agrees.</li>
            <li><Icon name="check" size={15} /> Secret-shaped output is redacted from the durable audit tail.</li>
          </ul>
          {!snapshot.app.pty && (
            <div className="inline-alert bad">
              <Icon name="alert" size={17} />
              <span>This Python build has no PTY support, so an interactive sign-in cannot be hosted.</span>
            </div>
          )}
          <Button icon="terminal" onClick={start} disabled={busy === "start" || !snapshot.app.pty}>
            {busy === "start" ? "Opening PTY…" : "Open sign-in terminal"}
          </Button>

          {history.length > 0 && (
            <div className="auth-history">
              <h4>Previous sign-in attempts</h4>
              {history.slice(0, 5).map((row) => (
                <button key={row.id} className="history-row" onClick={() => void openHistory(row.id)}>
                  <Pill tone={row.outcome === "authenticated" ? "green" : row.outcome === "cancelled" ? "amber" : "neutral"}>
                    {row.outcome || row.status}
                  </Pill>
                  <span className="mono">{row.mode}</span>
                  <span className="muted">{timeAgo(row.started_at)}</span>
                  <span className="ellipsis">{row.auth_detail || row.command}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {started && view && (
        <>
          <div className="terminal-meta">
            <Pill tone={outcomeTone}>{live ? `PTY ${view.mode}` : view.outcome || view.status}</Pill>
            {view.auth_status && <Pill tone={view.auth_status === "authenticated" ? "green" : view.auth_status === "unauthenticated" ? "amber" : "neutral"}>{view.auth_status}</Pill>}
            {view.finished && <span className="small muted">Recorded {timeAgo(view.ended_at)}</span>}
            {live && <span className="small muted">Output offsets {offset}/{view.total_chars}</span>}
          </div>

          {view.challenge && live && (
            <div className="inline-alert warn challenge">
              <Icon name="shield" size={18} />
              <span>
                A human verification prompt (CAPTCHA, OTP or MFA) is visible. The app has paused
                automation. Complete it yourself using the keys below or your own keyboard; nothing
                will be typed for you.
              </span>
            </div>
          )}

          <div className="terminal" ref={outputRef} tabIndex={0}>
            <pre>{cleanTerminal(view.text || "") || "Waiting for the CLI to write output…"}</pre>
          </div>

          {live && (
            <div className="terminal-controls">
              <div className="terminal-input">
                <input
                  value={text}
                  onChange={(event) => setText(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      void send({ text: text + "\r" });
                    } else if (event.key === "Tab") {
                      event.preventDefault();
                      void send({ key: "tab" });
                    }
                  }}
                  placeholder="Type only what the CLI asks for. Enter sends with a newline."
                  autoComplete="off"
                  spellCheck={false}
                  type="text"
                />
                <Button size="sm" variant="secondary" onClick={() => void send({ text })} disabled={!text || Boolean(busy)}>
                  Send text
                </Button>
              </div>
              <div className="key-row">
                {(["enter", "tab", "escape", "up", "down", "backspace", "ctrl_c"] as const).map((key) => (
                  <button key={key} className="key-btn" onClick={() => void send({ key })} disabled={Boolean(busy)}>
                    {key === "ctrl_c" ? "Ctrl+C" : key === "backspace" ? "⌫" : key === "escape" ? "Esc" : key === "enter" ? "Enter" : key === "tab" ? "Tab" : key === "up" ? "↑" : "↓"}
                  </button>
                ))}
              </div>
              <p className="small muted">
                Password fields do not echo; that is the CLI, not a frozen session. The local ad-hoc
                input is forwarded as raw PTY bytes.
              </p>
            </div>
          )}

          {finished && (
            <div className={`auth-result ${view.outcome === "authenticated" ? "ok" : "warn"}`}>
              <div>
                <strong>
                  {view.outcome === "authenticated"
                    ? "CLI status confirms you are signed in."
                    : view.outcome === "cancelled"
                      ? "Session cancelled by the human."
                      : "The session ended without an authenticated status."}
                </strong>
                <p>{view.auth_detail || "The CLI status command is the only source of truth here."}</p>
              </div>
              {view.outcome === "authenticated" && (
                <Button
                  size="sm"
                  variant="secondary"
                  icon="check"
                  onClick={() => { onDone(); onClose(); }}
                >
                  Done
                </Button>
              )}
            </div>
          )}

          {history.length > 1 && !live && (
            <details className="auth-history-details">
              <summary>Other attempts ({history.length})</summary>
              {history.map((row) => (
                <button key={row.id} className="history-row" onClick={() => void openHistory(row.id)}>
                  <Pill tone={row.outcome === "authenticated" ? "green" : row.outcome === "cancelled" ? "amber" : "neutral"}>
                    {row.outcome || row.status}
                  </Pill>
                  <span className="mono">{row.mode}</span>
                  <span className="muted">{timeAgo(row.started_at)}</span>
                </button>
              ))}
            </details>
          )}
        </>
      )}

      {error && <div className="pair-error"><Icon name="alert" size={17} /><div><strong>Sign-in request failed</strong><span>{error}</span></div></div>}
      {!started && history.length === 0 && (
        <p className="small muted">No sign-in attempts recorded yet. The timeline is kept locally and capped at 40 rows.</p>
      )}
      {started && !view && <p className="muted">Loading session…</p>}
      {sessionId && !view && <p className="muted">Loading recorded session…</p>}
    </Modal>
  );
}
