export type Availability = "detected" | "authenticated" | "usable" | "unknown";

export interface AgentModel {
  provider: string;
  model_id: string;
  display_name: string;
  availability: Availability;
}

export interface Agent {
  kind: string;
  display_name: string;
  binary: string;
  installed: boolean;
  path: string;
  version: string;
  availability: Availability;
  auth_status: string;
  auth_detail: string;
  models: AgentModel[];
  error: string;
  checked_at: string;
  script_url: string;
  npm_package: string;
  docs_url: string;
  known_paths: string[];
  platform: { os?: string; arch?: string; supported?: boolean; detail?: string };
  usable_verified_at: string;
}

export interface EventRecord {
  id: string;
  at: string;
  kind: string;
  level: string;
  message: string;
  fields: Record<string, unknown>;
}

export interface WorkerState {
  status: string;
  owner: string;
  pid: number | null;
  started_at: string;
  last_heartbeat_at: string;
  last_hello_at: string;
  hello: Record<string, unknown>;
  reconnect: { attempt?: number; delay_seconds?: number; at?: string } | null;
  error: string;
  tasks_pending: number;
  unit_active: boolean;
  logs: Array<{ at: string; level: string; text: string }>;
}

export interface Connection {
  state: string;
  paired: boolean;
  server_url: string;
  worker_id: string;
  worker_name: string;
  token_fingerprint: string;
  token_backend: string;
  token_warning: string;
  last_heartbeat_at: string;
  last_hello_at: string;
  latency_ms: number | null;
  checked_at: string;
  error: string;
  protocol_version: number;
  protocol_match: boolean | null;
  insecure_http: boolean;
  insecure_warning: string;
  operator_mode: boolean;
  operator_token_source: string;
  me: Record<string, any>;
  server_status: { version: string; service: string };
}

export interface TaskRecord {
  id: string;
  project_id?: string;
  title?: string;
  description?: string;
  status?: string;
  local_status?: string;
  priority?: number;
  worker_id?: string;
  worker_name?: string;
  agent_kind?: string;
  model_id?: string;
  progress?: number;
  attempts?: number;
  output?: string;
  error?: string;
  message?: string;
  created_at?: string;
  updated_at?: string;
  started_at?: string;
  finished_at?: string;
  acceptance?: string[];
  dependencies?: string[];
}

export interface Project {
  id: string;
  name: string;
  description: string;
  mode: string;
  status: string;
  progress: number;
  workspace_path: string;
  git_repo: string;
  meta: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  tasks_total: number;
  tasks_completed: number;
  tasks_running: number;
}

export interface Approval {
  id: string;
  project_id: string;
  stage: string;
  summary: string;
  details: Record<string, unknown>;
  status: string;
  requested_at: string;
  decided_at: string | null;
  note: string;
}

export interface ApprovalStage {
  state: string;
  approval_id: string;
  summary: string;
  requested_at: string;
  note: string;
  history: Approval[];
  ui_state: string;
}

export interface HumanAction {
  id: string;
  project_id: string;
  title: string;
  description: string;
  why: string;
  feature: string;
  credential_name: string;
  status: string;
  blocking: boolean;
  created_at: string;
  resolved_at: string | null;
}

export interface ProjectDetail extends Project {
  blueprint: any;
  ui_spec: any;
  approvals: Approval[];
  human_actions: HumanAction[];
  secrets: Array<{ name: string; present?: boolean }>;
  approval_states: Record<string, ApprovalStage>;
}

export interface Artifact {
  id: string;
  task_id: string | null;
  kind: string;
  name: string;
  path: string;
  sha256: string;
  size: number;
  verified: boolean;
  created_at: string;
}

export interface ServerEvent {
  id?: number;
  type?: string;
  kind?: string;
  created_at?: string;
  at?: string;
  payload?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface SystemStats {
  cpu_percent: number | null;
  mem_total_bytes: number | null;
  mem_used_bytes: number | null;
  mem_percent: number | null;
  load: number[] | null;
  cpu_count: number | null;
  sampled_at: string;
}

export interface ArtifactRecord {
  id: string;
  task_id: string | null;
  kind: string;
  name: string;
  path: string;
  sha256: string;
  size: number;
  verified: boolean;
  created_at: string;
}

export interface WorkspaceStatus {
  path?: string;
  exists?: boolean;
  is_repo?: boolean;
  branch?: string;
  head?: string;
  changed?: number;
  untracked?: number;
  ahead?: number;
  behind?: number;
  error?: string;
  [key: string]: unknown;
}

export interface WorkspaceDiff {
  path?: string;
  diff?: string;
  stat?: string;
  truncated?: boolean;
  error?: string;
  [key: string]: unknown;
}

export interface DeliveryRun {
  id: string;
  project_id?: string;
  command?: string;
  status?: string;
  exit_code?: number | null;
  output?: string;
  error?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface Snapshot {
  app: {
    name: string;
    version: string;
    protocol_version: number;
    started_at: string;
    platform: string;
    arch: string;
    pty: boolean;
    python: string;
    paths: Record<string, string>;
    device_key_fingerprint: string;
  };
  connection: Connection;
  system: SystemStats;
  worker: WorkerState;
  agents: Agent[];
  tasks: TaskRecord[];
  events: EventRecord[];
  auth_history: AuthSessionSummary[];
  operator: {
    available: boolean;
    error: string;
    last_refresh: string;
    counts: Record<string, number>;
    projects: Project[];
    workers: Array<Record<string, any>>;
    documents: Array<Record<string, any>>;
    events: ServerEvent[];
    protocol_gaps: string[];
  };
  security: {
    insecure_http: boolean;
    secret_backend: string;
    secret_tool_available: boolean;
    has_worker_token: boolean;
    has_operator_token: boolean;
  };
}

export interface AuthSessionSummary {
  id: string;
  kind: string;
  command: string;
  mode: string;
  status: string;
  exit_code: number | null;
  auth_status: string;
  auth_detail: string;
  outcome: string;
  challenge: boolean;
  challenge_text: string;
  tail: string;
  started_at: string;
  ended_at: string;
}

export interface Job {
  id: string;
  kind: string;
  operation: string;
  status: string;
  stage: string;
  message: string;
  log: Array<{ at: string; text: string }>;
  next_offset: number;
  result: Record<string, any> | null;
  error: string;
  started_at: string;
  ended_at: string;
  log_path: string;
}

export interface AuthSessionView {
  id: string;
  kind: string;
  command: string;
  mode: string;
  status: string;
  exit_code: number | null;
  error: string | null;
  text: string;
  next_offset: number;
  first_offset: number;
  total_chars: number;
  truncated: boolean;
  started_at: string;
  ended_at: string | null;
  timeout_seconds: number;
  challenge: boolean;
  auth_status?: string;
  auth_detail?: string;
  outcome?: string;
  finished?: boolean;
}
