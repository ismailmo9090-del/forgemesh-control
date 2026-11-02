export interface BridgeResponse<T = any> {
  ok: boolean;
  status: number;
  data: T;
}

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(message: string, code = "request_failed", status = 0) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

declare global {
  interface Window {
    forgemeshClient?: {
      request: (method: string, path: string, body?: unknown) => Promise<BridgeResponse>;
      info: () => Promise<Record<string, unknown>>;
      autostart: () => Promise<{ enabled: boolean; path: string }>;
      setAutostart: (enabled: boolean) => Promise<{ enabled: boolean; path: string }>;
      openLog: () => Promise<string>;
      onEvent: (callback: (event: unknown) => void) => () => void;
      onDaemonState: (callback: (state: "up" | "down") => void) => () => void;
      quit: () => Promise<void>;
      hide: () => Promise<void>;
    };
  }
}

function bridge() {
  if (!window.forgemeshClient) {
    throw new ApiError(
      "The Electron preload bridge is unavailable. Start the Client Manager with `npm start` in client/desktop.",
      "no_bridge"
    );
  }
  return window.forgemeshClient;
}

export async function request<T = any>(
  method: string,
  path: string,
  body?: unknown
): Promise<T> {
  const response = await bridge().request(method, path, body);
  if (!response.ok) {
    const detail = response.data?.detail;
    const message =
      (detail && typeof detail === "object" ? detail.message : detail) ||
      response.data?.message ||
      `Request failed with HTTP ${response.status}`;
    const code =
      (detail && typeof detail === "object" ? detail.code : "") ||
      response.data?.code ||
      "request_failed";
    throw new ApiError(String(message), String(code), response.status);
  }
  return response.data as T;
}

export const api = {
  state: () => request("GET", "/api/state"),
  pair: (body: unknown) => request("POST", "/api/pair", body),
  unpair: (body: unknown) => request("POST", "/api/unpair", body),
  config: (body: unknown) => request("POST", "/api/config", body),
  connectionTest: () => request("POST", "/api/connection/test"),
  startWorker: (takeover = false) => request("POST", "/api/worker/start", { takeover }),
  stopWorker: (stopUnit = false) => request("POST", "/api/worker/stop", { confirm: true, stop_unit: stopUnit }),
  restartWorker: (takeover = false) => request("POST", "/api/worker/restart", { takeover }),
  detectAgent: (kind: string) => request("POST", `/api/agents/${kind}/detect`),
  installAgent: (kind: string, force = false) =>
    request("POST", `/api/agents/${kind}/install`, { force }),
  updateAgent: (kind: string) => request("POST", `/api/agents/${kind}/update`, { approve: true }),
  verifyAgent: (kind: string) => request("POST", `/api/agents/${kind}/verify`),
  job: (id: string, offset = 0) => request("GET", `/api/jobs/${id}?offset=${offset}`),
  startLogin: (kind: string, provider = "") =>
    request("POST", `/api/agents/${kind}/login`, { provider, cols: 110, rows: 30 }),
  authSession: (kind: string, id: string, offset = 0) =>
    request("GET", `/api/agents/${kind}/auth/session?session_id=${encodeURIComponent(id)}&offset=${offset}`),
  authInput: (kind: string, sessionId: string, text: string) =>
    request("POST", `/api/agents/${kind}/auth/session/input`, { session_id: sessionId, text }),
  authKey: (kind: string, sessionId: string, key: string) =>
    request("POST", `/api/agents/${kind}/auth/session/key`, { session_id: sessionId, key }),
  authCancel: (kind: string, sessionId: string) =>
    request("POST", `/api/agents/${kind}/auth/session/cancel`, { session_id: sessionId }),
  logout: (kind: string, provider = "") =>
    request("POST", `/api/agents/${kind}/logout`, { provider }),
  openTerminal: (kind: string, provider = "") =>
    request("POST", `/api/agents/${kind}/open-terminal`, { provider }),
  reportInventory: () => request("POST", "/api/report/inventory"),
  enableOperatorLocal: () => request("POST", "/api/operator/enable", { use_local: true }),
  enableOperator: (token: string) => request("POST", "/api/operator/enable", { token }),
  disableOperator: () => request("POST", "/api/operator/disable"),
  projects: () => request("GET", "/api/operator/projects"),
  project: (id: string) => request("GET", `/api/operator/projects/${id}`),
  gate: (id: string, summary = "") =>
    request("POST", `/api/operator/projects/${id}/gate`, { summary }),
  decide: (projectId: string, approvalId: string, approved: boolean, note: string) =>
    request("POST", `/api/operator/projects/${projectId}/approvals/${approvalId}/decide`, {
      approved,
      note
    }),
  humanAction: (projectId: string, body: unknown) =>
    request("POST", `/api/operator/projects/${projectId}/human-actions`, body),
  resolveAction: (projectId: string, actionId: string, status: string, note = "") =>
    request("POST", `/api/operator/projects/${projectId}/human-actions/${actionId}/resolve`, {
      status,
      note
    }),
  tasks: () => request("GET", "/api/operator/tasks"),
  task: (id: string) => request("GET", `/api/operator/tasks/${id}`),
  workers: () => request("GET", "/api/operator/workers"),
  events: () => request("GET", "/api/operator/events"),
  documents: () => request("GET", "/api/operator/documents"),
  document: (id: string) => request("GET", `/api/operator/documents/${id}`),
  artifacts: (projectId: string) =>
    request("GET", `/api/operator/artifacts?project_id=${encodeURIComponent(projectId)}`),
  collectArtifacts: (projectId: string) =>
    request("POST", `/api/operator/artifacts/collect?project_id=${encodeURIComponent(projectId)}`),
  runs: (projectId: string) =>
    request("GET", `/api/operator/runs?project_id=${encodeURIComponent(projectId)}`),
  verifyArtifact: (projectId: string, artifactId: string) =>
    request("POST", "/api/operator/artifacts/verify", {
      project_id: projectId,
      artifact_id: artifactId
    }),
  copyArtifact: (projectId: string, artifactId: string) =>
    request("POST", "/api/operator/artifacts/copy", {
      project_id: projectId,
      artifact_id: artifactId,
      confirm: true
    }),
  deliveryRun: (body: unknown) => request("POST", "/api/operator/delivery/run", body),
  deliveryReport: (projectId: string, kind = "review") =>
    request("POST", "/api/operator/delivery/report", { project_id: projectId, kind }),
  workspaceStatus: (path: string) =>
    request("GET", `/api/operator/workspaces/status?path=${encodeURIComponent(path)}`),
  workspaceDiff: (path: string) =>
    request("GET", `/api/operator/workspaces/diff?path=${encodeURIComponent(path)}`),
  workspaceLog: (path: string) =>
    request("GET", `/api/operator/workspaces/log?path=${encodeURIComponent(path)}`),
  disk: () => request("GET", "/api/system/disk"),
  updateCheck: () => request("GET", "/api/system/update-check")
};

export const shell = {
  info: () => bridge().info(),
  autostart: () => bridge().autostart(),
  setAutostart: (enabled: boolean) => bridge().setAutostart(enabled),
  openLog: () => bridge().openLog(),
  onEvent: (cb: (event: unknown) => void) => bridge().onEvent(cb),
  onDaemonState: (cb: (state: "up" | "down") => void) => bridge().onDaemonState(cb),
  quit: () => bridge().quit(),
  hide: () => bridge().hide()
};
