import { useEffect, useRef, useState } from "react";

type IconName =
  | "dashboard"
  | "agent"
  | "tasks"
  | "build"
  | "gate"
  | "logs"
  | "settings"
  | "about"
  | "server"
  | "laptop"
  | "cpu"
  | "memory"
  | "activity"
  | "wifi"
  | "check"
  | "alert"
  | "clock"
  | "copy"
  | "link"
  | "shield"
  | "power"
  | "play"
  | "stop"
  | "refresh"
  | "terminal"
  | "download"
  | "hash"
  | "arrow"
  | "close"
  | "external"
  | "folder"
  | "info"
  | "eye";

const PATHS: Record<IconName, string> = {
  dashboard: "M3 10.5 12 3l9 7.5V21H3z M9 21v-7h6v7",
  agent: "M9 3h6v3h3a2 2 0 0 1 2 2v3h-3v6h3v3a2 2 0 0 1-2 2h-3v-3H9v3H6a2 2 0 0 1-2-2v-3h3V8H4V6a2 2 0 0 1 2-2h3z",
  tasks: "M4 4h16v16H4z M8 9l2 2 4-4 M8 16h8",
  build: "M12 3 3 7.5 12 12l9-4.5z M3 12l9 4.5 9-4.5 M3 16.5 12 21l9-4.5",
  gate: "M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z M9 12l2 2 4-4",
  logs: "M5 4h14v16H5z M8 8h8 M8 12h8 M8 16h5",
  settings: "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.4 1a7 7 0 0 0-1.7-1L14.4 3h-4.8l-.4 3a7 7 0 0 0-1.7 1l-2.4-1-2 3.5L5 11a7 7 0 0 0 0 2l-2 1.5 2 3.4 2.4-1a7 7 0 0 0 1.7 1l.4 3h4.8l.4-3a7 7 0 0 0 1.7-1l2.4 1 2-3.4-2-1.5a7 7 0 0 0 .1-1z",
  about: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 11v6 M12 8h.01",
  server: "M4 4h16v6H4z M4 14h16v6H4z M7 7h.01 M7 17h.01",
  laptop: "M4 5h16v11H4z M2 19h20",
  cpu: "M7 7h10v10H7z M9 2v3 M15 2v3 M9 19v3 M15 19v3 M2 9h3 M2 15h3 M19 9h3 M19 15h3",
  memory: "M5 7h14v8H5z M7 15v3 M12 15v3 M17 15v3 M8 10h2 M14 10h2",
  activity: "M3 12h4l2-7 4 14 2-7h6",
  wifi: "M2 8.5a15 15 0 0 1 20 0 M5 12a10 10 0 0 1 14 0 M8.5 15.5a5 5 0 0 1 7 0 M12 20h.01",
  check: "M20 6 9 17l-5-5",
  alert: "M12 3 2 21h20z M12 9v5 M12 18h.01",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 7v5l3 2",
  copy: "M9 9h10v12H9z M5 15H3V3h12v2",
  link: "M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1 M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1",
  shield: "M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z",
  power: "M12 2v10 M6.5 5.5a8 8 0 1 0 11 0",
  play: "M8 5v14l11-7z",
  stop: "M6 6h12v12H6z",
  refresh: "M20 11a8 8 0 1 0-2 6 M20 4v7h-7",
  terminal: "M3 4h18v16H3z M7 9l3 3-3 3 M13 15h4",
  download: "M12 3v12 M7 10l5 5 5-5 M4 21h16",
  hash: "M9 3 7 21 M17 3l-2 18 M4 9h17 M3 15h17",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  close: "M6 6l12 12 M18 6 6 18",
  external: "M14 3h7v7 M10 14 21 3 M21 14v7H3V3h7",
  folder: "M3 6h7l2 2h9v13H3z",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 10v6 M12 7h.01",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"
};

export function Icon({ name, size = 20, className = "" }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg
      className={`icon ${className}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`brand ${compact ? "compact" : ""}`}>
      <div className="brand-mark" aria-hidden>
        <span className="b1" />
        <span className="b2" />
        <span className="b3" />
      </div>
      {!compact && (
        <div className="brand-text">
          <strong>ForgeMesh</strong>
          <span>Client Manager</span>
        </div>
      )}
    </div>
  );
}

export function Button({
  children,
  variant = "primary",
  size = "md",
  icon,
  onClick,
  disabled,
  title,
  type = "button"
}: {
  children?: React.ReactNode;
  variant?: "primary" | "secondary" | "ghost" | "danger" | "success";
  size?: "sm" | "md" | "lg";
  icon?: IconName;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
  type?: "button" | "submit";
}) {
  return (
    <button
      className={`btn ${variant} ${size}`}
      onClick={onClick}
      disabled={disabled}
      title={title}
      type={type}
    >
      {icon && <Icon name={icon} size={size === "sm" ? 14 : 16} />}
      {children && <span>{children}</span>}
    </button>
  );
}

export function Pill({
  tone = "neutral",
  children,
  dot = true
}: {
  tone?: "green" | "amber" | "red" | "blue" | "neutral" | "violet";
  children: React.ReactNode;
  dot?: boolean;
}) {
  return (
    <span className={`pill ${tone}`}>
      {dot && <i />}
      {children}
    </span>
  );
}

export function Progress({ value, tone = "blue" }: { value: number; tone?: "blue" | "green" | "amber" | "red" }) {
  const width = Math.max(0, Math.min(100, Number(value) || 0));
  return (
    <div className={`progress ${tone}`} role="progressbar" aria-valuenow={width} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${width}%` }} />
    </div>
  );
}

export function Sparkline({ values, tone = "blue" }: { values: number[]; tone?: "blue" | "green" | "violet" }) {
  const data = values.length ? values : [2, 3, 2.4, 4, 3.2, 4.6, 3.8];
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const points = data
    .map((value, index) => {
      const x = (index / Math.max(data.length - 1, 1)) * 100;
      const y = 32 - ((value - min) / span) * 24;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg className={`sparkline ${tone}`} viewBox="0 0 100 36" preserveAspectRatio="none" aria-hidden>
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="2" />
      <polygon points={`0,36 ${points} 100,36`} fill="currentColor" opacity="0.10" />
    </svg>
  );
}

export function Modal({
  title,
  subtitle,
  children,
  onClose,
  wide = false,
  footer
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  onClose: () => void;
  wide?: boolean;
  footer?: React.ReactNode;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <header>
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            <Icon name="close" size={18} />
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

export function Empty({ title, detail, action }: { title: string; detail: string; action?: React.ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Icon name="info" size={22} />
      </div>
      <strong>{title}</strong>
      <p>{detail}</p>
      {action}
    </div>
  );
}

export function Field({
  label,
  hint,
  children
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}

export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      if (timer.current) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return (
    <button className="copy-btn" onClick={copy} title={label}>
      <Icon name={copied ? "check" : "copy"} size={14} />
      {copied ? "Copied" : label}
    </button>
  );
}

export function SectionHeader({
  title,
  subtitle,
  action
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="section-head">
      <div>
        <h2>{title}</h2>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

export function StatusBanner({
  state,
  title,
  detail,
  children
}: {
  state: "ok" | "warn" | "bad" | "info";
  title: string;
  detail: string;
  children?: React.ReactNode;
}) {
  return (
    <div className={`status-banner ${state}`}>
      <div className="status-round">
        <Icon name={state === "ok" ? "check" : state === "bad" ? "alert" : state === "warn" ? "alert" : "info"} size={22} />
      </div>
      <div className="status-copy">
        <strong>{title}</strong>
        <span>{detail}</span>
      </div>
      {children}
    </div>
  );
}

export function formatBytes(value: number | null | undefined): string {
  if (!value && value !== 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function timeAgo(value?: string | null): string {
  if (!value) return "—";
  const stamp = Date.parse(value);
  if (Number.isNaN(stamp)) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function formatDate(value?: string | null): string {
  if (!value) return "—";
  const stamp = Date.parse(value);
  if (Number.isNaN(stamp)) return value;
  return new Date(stamp).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export function shortSha(value?: string | null): string {
  if (!value) return "—";
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

export function taskStatusLabel(status?: string): string {
  const map: Record<string, string> = {
    queued: "Queued",
    assigned: "Assigned",
    running: "Running",
    blocked: "Blocked",
    awaiting_approval: "Awaiting approval",
    interrupted: "Interrupted",
    cancelled: "Cancelled",
    completed: "Completed",
    succeeded: "Completed",
    failed: "Failed",
    draft: "Draft"
  };
  return map[status || ""] || status || "Unknown";
}

export function taskStatusTone(status?: string): "green" | "amber" | "red" | "blue" | "neutral" {
  if (status === "completed" || status === "succeeded") return "green";
  if (status === "failed" || status === "cancelled" || status === "interrupted") return "red";
  if (status === "running" || status === "assigned") return "blue";
  if (status === "queued" || status === "awaiting_approval" || status === "blocked") return "amber";
  return "neutral";
}

export function availabilityTone(availability?: string): "green" | "amber" | "blue" | "neutral" {
  if (availability === "usable") return "green";
  if (availability === "authenticated") return "blue";
  if (availability === "detected") return "amber";
  return "neutral";
}

export function connectionTone(state?: string): "green" | "amber" | "red" | "blue" | "neutral" {
  if (state === "connected") return "green";
  if (state === "reconnecting" || state === "connecting") return "amber";
  if (state === "revoked" || state === "protocol_mismatch" || state === "insecure_transport_warning") return "red";
  if (state === "offline") return "amber";
  return "neutral";
}

export function connectionLabel(state?: string): string {
  const map: Record<string, string> = {
    connected: "Worker Connected",
    connecting: "Connecting…",
    reconnecting: "Reconnecting",
    offline: "Worker offline",
    unpaired: "Not paired",
    revoked: "Worker revoked",
    protocol_mismatch: "Protocol mismatch",
    insecure_transport_warning: "Insecure transport"
  };
  return map[state || ""] || state || "Unknown";
}

export function uptime(startedAt?: string): string {
  if (!startedAt) return "—";
  const start = Date.parse(startedAt);
  if (Number.isNaN(start)) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m ${seconds % 60}s`;
}
