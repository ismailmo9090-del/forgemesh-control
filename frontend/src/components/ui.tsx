import { useEffect, useRef, type ReactNode } from "react";

export function Sparkline({
  values,
  color = "var(--accent)",
  height = 44,
  fill = true
}: {
  values: number[];
  color?: string;
  height?: number;
  fill?: boolean;
}) {
  const data = values.length > 1 ? values.slice(-60) : [0, 0];
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const span = max - min || 1;
  const points = data.map((value, index) => {
    const x = (index / (data.length - 1)) * 100;
    const y = height - ((value - min) / span) * (height - 6) - 3;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
  const area = `0,${height} ${points.join(" ")} 100,${height}`;
  return (
    <svg
      className="sparkline"
      style={{ color }}
      viewBox={`0 0 100 ${height}`}
      preserveAspectRatio="none"
      aria-hidden
    >
      {fill && <polygon points={area} fill="currentColor" opacity="0.10" />}
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

const STATUS_LABELS: Record<string, string> = {
  not_detected: "Not Detected",
  detected: "Detected",
  installing: "Installing",
  verifying: "Verifying",
  ready: "Ready",
  failed: "Failed",
  disabled: "Disabled",
  available: "Available",
  limited: "Limited",
  unavailable: "Unavailable",
  authenticated: "Authenticated",
  unauthenticated: "Not signed in",
  unknown: "Unknown",
  online: "Online",
  offline: "Offline",
  idle: "Idle",
  running: "Running",
  queued: "Queued",
  completed: "Completed"
};

export function StatusChip({
  status,
  label,
  tone,
  title
}: {
  status: string;
  label?: string;
  tone?: "ok" | "warn" | "bad" | "muted" | "info";
  title?: string;
}) {
  const resolved =
    tone ||
    (["ready", "available", "authenticated", "online", "completed"].includes(status)
      ? "ok"
      : ["limited", "unauthenticated", "detected", "installing", "verifying", "queued", "idle", "running"].includes(
            status
          )
        ? "warn"
        : ["failed", "unavailable", "offline"].includes(status)
          ? "bad"
          : "muted");
  return (
    <span className={`chip chip-${resolved}`} title={title}>
      {label || STATUS_LABELS[status] || status}
    </span>
  );
}

// "limited" covers two different things: a real CLI limitation, and an
// installed agent whose functional prompt has simply not run yet. Only the
// second one is labelled "unverified" so nobody reads it as a blocker.
function awaitingFunctionalCheck(
  status: string,
  functional?: { attempted?: boolean; verified?: boolean }
): boolean {
  return status === "limited" && functional?.attempted === false;
}

export function availabilityText(
  status: string,
  functional?: { attempted?: boolean; verified?: boolean }
): string {
  return awaitingFunctionalCheck(status, functional)
    ? "unverified"
    : STATUS_LABELS[status] || status;
}

export function AvailabilityChip({
  status,
  functional
}: {
  status: string;
  functional?: { attempted?: boolean; verified?: boolean };
}) {
  const unverified = awaitingFunctionalCheck(status, functional);
  return (
    <StatusChip
      status={status}
      tone={unverified ? "info" : undefined}
      label={unverified ? "unverified" : undefined}
      title={
        unverified
          ? "Installed and runnable. No real prompt has been verified yet; ForgeMesh still lets the Council use it and reports the CLI's own result."
          : undefined
      }
    />
  );
}

export function Card({
  title,
  subtitle,
  action,
  live,
  children,
  className = ""
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
  live?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      <header className="card-head">
        <div className="card-title">
          <span className="card-glyph" aria-hidden>
            {typeof title === "string" ? title.slice(0, 1).toUpperCase() : "•"}
          </span>
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
        </div>
        <div className="card-actions">
          {live && <span className="live"><i /> Live</span>}
          {action}
        </div>
      </header>
      {children}
    </section>
  );
}

export function Modal({
  title,
  children,
  onClose,
  width = "760px"
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  width?: string;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  // Keep the latest handler without re-running the focus effect whenever the
  // parent re-renders (the dashboard refreshes every few seconds).
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = dialog.current;
    const focusable = () =>
      node
        ? Array.from(
            node.querySelectorAll<HTMLElement>(
              'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
            )
          ).filter(
            (item) =>
              !item.hasAttribute("disabled") &&
              item.getAttribute("aria-hidden") !== "true" &&
              item.getClientRects().length > 0
          )
        : [];
    const first = focusable()[0];
    (first || node)?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (items.length === 0) return;
      const head = items[0];
      const tail = items[items.length - 1];
      if (event.shiftKey && document.activeElement === head) {
        event.preventDefault();
        tail.focus();
      } else if (!event.shiftKey && document.activeElement === tail) {
        event.preventDefault();
        head.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previous?.focus?.();
    };
  }, []);

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close.current();
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={dialog}
        style={{ maxWidth: width }}
      >
        <header className="modal-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  );
}

// Distinct, dark enough to read as a glyph on the light surfaces.
export const PROVIDER_META: Record<string, { label: string; glyph: string; color: string }> = {
  chatgpt: { label: "ChatGPT", glyph: "◎", color: "#4a5561" },
  deepseek: { label: "DeepSeek", glyph: "◉", color: "#2f6bdf" },
  claude: { label: "Claude", glyph: "✳", color: "#b4502a" },
  gemini: { label: "Gemini", glyph: "✦", color: "#6b46c1" },
  perplexity: { label: "Perplexity", glyph: "⌘", color: "#0f7b7b" },
  sarvam: { label: "Sarvam", glyph: "❀", color: "#a83d68" }
};
