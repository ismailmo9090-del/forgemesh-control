const TOKEN_KEY = "fmc_control_token";

function fromHash(): string {
  const match = window.location.hash.match(/token=([^&]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export function storedToken(): string {
  const fromUrl = fromHash();
  if (fromUrl) {
    localStorage.setItem(TOKEN_KEY, fromUrl);
    history.replaceState(null, "", window.location.pathname + window.location.search);
    return fromUrl;
  }
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function saveToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** FastAPI returns ``detail`` as a string, but validation errors are arrays. */
function readError(body: unknown, fallback: string): string {
  if (!body || typeof body !== "object") return fallback;
  const record = body as Record<string, unknown>;
  const detail = record.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (!item || typeof item !== "object") return "";
        const entry = item as Record<string, unknown>;
        const location = Array.isArray(entry.loc)
          ? entry.loc.filter((part) => part !== "body").join(".")
          : "";
        const message = typeof entry.msg === "string" ? entry.msg : "";
        return location && message ? `${location}: ${message}` : message;
      })
      .filter(Boolean);
    if (parts.length > 0) return parts.join("; ");
  }
  if (detail && typeof detail === "object") {
    const message = (detail as Record<string, unknown>).message;
    if (typeof message === "string") return message;
    return JSON.stringify(detail);
  }
  const error = record.error;
  if (typeof error === "string") return error;
  if (error && typeof error === "object") {
    const message = (error as Record<string, unknown>).message;
    if (typeof message === "string") return message;
  }
  return fallback;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = storedToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("X-API-Token", token);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "include"
  });
  if (!response.ok) {
    const fallback = `${response.status} ${response.statusText}`;
    let message = fallback;
    try {
      message = readError(await response.json(), fallback);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return {} as T;
  return (await response.json()) as T;
}

export function websocketUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const token = storedToken();
  const params = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${protocol}//${window.location.host}/ws${params}`;
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export function formatTime(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
