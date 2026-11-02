import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "./api";
import { Card, StatusChip } from "./components/ui";
import type { AgentCliAccount, AgentInfo, AgentTerminalView } from "./types";

/**
 * Coding-agent sign-in is hosted, not simulated: the control server spawns the
 * CLI's own documented login command in a PTY and this panel shows that real
 * screen. The CLI's status, not this component, decides when a sign-in worked.
 */

const POLL_MS = 1200;

const KEY_ROWS: Array<Array<{ label: string; key: string }>> = [
  [
    { label: "Enter", key: "enter" },
    { label: "Tab", key: "tab" },
    { label: "Esc", key: "escape" },
    { label: "Backspace", key: "backspace" },
    { label: "Space", key: "space" }
  ],
  [
    { label: "↑", key: "up" },
    { label: "↓", key: "down" },
    { label: "←", key: "left" },
    { label: "→", key: "right" },
    { label: "Ctrl+C", key: "ctrl_c" },
    { label: "Ctrl+D", key: "ctrl_d" }
  ]
];

function linkify(text: string) {
  return text.split(/(https?:\/\/[^\s"'<>]+)/g).map((part, index) =>
    /^https?:\/\//.test(part) ? (
      <a key={index} href={part} target="_blank" rel="noreferrer">
        {part}
      </a>
    ) : (
      <span key={index}>{part}</span>
    )
  );
}

export function HostedTerminal({
  agent,
  account,
  provider,
  resumeSessionId,
  onSettled
}: {
  agent: AgentInfo;
  account: AgentCliAccount | null;
  provider: string;
  resumeSessionId?: string | null;
  onSettled: () => void;
}) {
  const [view, setView] = useState<AgentTerminalView | null>(null);
  const [error, setError] = useState("");
  const [input, setInput] = useState("");
  const offset = useRef(0);
  const sessionId = useRef<string | null>(null);
  const screen = useRef<HTMLPreElement | null>(null);
  const timer = useRef<number | null>(null);
  const alive = useRef(true);
  const sending = useRef(false);
  const settled = useRef(onSettled);
  settled.current = onSettled;
  // The parent re-renders this panel whenever it refreshes its auth state, so
  // the launch target lives in a ref: only this component's own controls can
  // start a session, and a refresh can never restart a live login.
  const target = useRef({ kind: agent.kind, account_id: account?.id ?? null, provider, adopt: "" });
  target.current = {
    kind: agent.kind,
    account_id: account?.id ?? null,
    provider,
    adopt: resumeSessionId ?? ""
  };

  const pump = useCallback(
    (after: number) => {
      if (!alive.current || !sessionId.current) return;
      const kind = target.current.kind;
      if (timer.current) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(async () => {
        // Read the watermark at request time, not when the timer was armed:
        // a key/input push may have advanced it while this poll was waiting.
        const from = offset.current || after;
        try {
          const next = await api<AgentTerminalView>(
            `/api/agents/${kind}/auth/session?session_id=${encodeURIComponent(
              sessionId.current || ""
            )}&offset=${from}`
          );
          if (offset.current !== from) {
            // Another response already applied this range; re-read from the
            // current watermark instead of duplicating text.
            pump(offset.current);
            return;
          }
          setView((previous) => {
            if (!previous) return next;
            return {
              ...previous,
              text: next.truncated ? next.text : `${previous.text}${next.text}`,
              status: next.status,
              exit_code: next.exit_code,
              error: next.error,
              ended_at: next.ended_at
            };
          });
          offset.current = next.next_offset;
          if (["cancelled", "exited", "failed"].includes(next.status)) {
            await settled.current();
            return;
          }
          pump(offset.current);
        } catch (caught) {
          setError(caught instanceof ApiError ? caught.message : String(caught));
        }
      }, POLL_MS);
    },
    []
  );

  const start = useCallback(
    async (adoptRunning: boolean) => {
      if (timer.current) window.clearTimeout(timer.current);
      setError("");
      offset.current = 0;
      sessionId.current = null;
      setView(null);
      const { kind, account_id, provider: launchProvider, adopt } = target.current;
      try {
        if (adoptRunning && adopt) {
          // A login the CLI is still running belongs to this agent; show that
          // screen instead of starting a competing session.
          const existing = await api<AgentTerminalView>(
            `/api/agents/${kind}/auth/session?session_id=${encodeURIComponent(adopt)}&offset=0`
          );
          sessionId.current = existing.id;
          offset.current = existing.next_offset;
          setView(existing);
          if (!["cancelled", "exited", "failed"].includes(existing.status)) {
            pump(existing.next_offset);
          }
          return;
        }
        const started = await api<AgentTerminalView>(`/api/agents/${kind}/auth/login`, {
          method: "POST",
          body: JSON.stringify({
            provider: launchProvider,
            account_id,
            cols: 100,
            rows: 24
          })
        });
        sessionId.current = started.id;
        offset.current = started.next_offset;
        setView(started);
        if (started.status === "running") pump(started.next_offset);
      } catch (caught) {
        setError(caught instanceof ApiError ? caught.message : String(caught));
      }
    },
    [pump]
  );

  useEffect(() => {
    alive.current = true;
    void start(true);
    return () => {
      alive.current = false;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [start]);

  useEffect(() => {
    const node = screen.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [view?.text]);

  const push = async (endpoint: "input" | "key", payload: Record<string, unknown>) => {
    if (!sessionId.current || sending.current) return;
    sending.current = true;
    try {
      const next = await api<AgentTerminalView>(
        `/api/agents/${agent.kind}/auth/session/${endpoint}`,
        { method: "POST", body: JSON.stringify({ ...payload, session_id: sessionId.current }) }
      );
      setView((previous) =>
        previous
          ? {
              ...previous,
              text: next.truncated ? next.text : `${previous.text}${next.text}`,
              status: next.status
            }
          : previous
      );
      offset.current = Math.max(offset.current, next.next_offset);
      if (timer.current) window.clearTimeout(timer.current);
      pump(offset.current);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      sending.current = false;
    }
  };

  const cancel = async () => {
    if (!sessionId.current) return;
    try {
      await api(`/api/agents/${agent.kind}/auth/session/cancel`, {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId.current })
      });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
    await settled.current();
  };

  const running = view?.status === "running" || view?.status === "starting";
  const outcome =
    view && !running
      ? view.status === "cancelled"
        ? "Cancelled. The CLI reported no session, so no account was marked signed in."
        : view.status === "exited" && view.exit_code === 0
          ? "The login process exited cleanly. Its own status, shown below, decides the account state."
          : `The login process ended (status ${view.status}${
              view.exit_code === null ? "" : `, exit code ${view.exit_code}`
            })${view.error ? `: ${view.error}` : ". Nothing was marked signed in."}`
      : "";

  return (
    <Card
      title={`${agent.display_name} sign-in`}
      className="hosted-terminal"
      subtitle="The CLI's own login command, hosted in an embedded terminal."
      action={
        <div className="card-actions">
          <button className="btn small-btn" onClick={() => void start(false)}>
            Restart
          </button>
          <button className="btn small-btn" onClick={() => void cancel()} disabled={!running}>
            Cancel sign-in
          </button>
        </div>
      }
      live={running}
    >
      <div className="term-head">
        <StatusChip
          status={running ? "running" : view ? "idle" : "queued"}
          label={
            running
              ? "waiting for you in the CLI"
              : view
                ? `session ${view.status}`
                : "starting session"
          }
          tone={view?.status === "exited" ? "ok" : running ? "info" : "muted"}
        />
        <small className="mono">{view?.command || "spawning the documented login command…"}</small>
      </div>

      <pre className="terminal tall" ref={screen}>
        {view?.text
          ? linkify(view.text)
          : running
            ? "Waiting for the CLI's first screen…"
            : error || "Opening a hosted terminal for the CLI's own login command…"}
      </pre>

      {running && (
        <p className="info-box">
          The CLI is waiting on you: choose a menu entry with the arrow keys and confirm with
          Enter, or type in the box below - then finish any browser step it prints above.
        </p>
      )}

      {error && <p className="error-box">{error}</p>}
      {outcome && <p className="info-box">{outcome}</p>}

      <div className="term-keys">
        {KEY_ROWS.map((row) => (
          <div className="key-row" key={row.map((item) => item.key).join("-")}>
            {row.map((item) => (
              <button
                className="btn small-btn"
                key={item.key}
                disabled={!running}
                onClick={() => void push("key", { key: item.key })}
              >
                {item.label}
              </button>
            ))}
          </div>
        ))}
        <form
          className="term-input"
          onSubmit={(event) => {
            event.preventDefault();
            const text = input;
            setInput("");
            void push("input", { text }).then(() => void push("key", { key: "enter" }));
          }}
        >
          <input
            value={input}
            disabled={!running}
            placeholder="type into the live CLI session, then send"
            onChange={(event) => setInput(event.target.value)}
          />
          <button className="btn small-btn primary" type="submit" disabled={!running || !input}>
            Send
          </button>
        </form>
      </div>

      <p className="desktop-note">
        Complete the provider's own flow - URL, device code, OTP or MFA - and never paste a password
        here. ForgeMesh records only what the CLI reports afterwards.
      </p>
    </Card>
  );
}
