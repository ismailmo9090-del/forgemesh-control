export type AgentPhase =
  | "not_detected"
  | "detected"
  | "installing"
  | "verifying"
  | "ready"
  | "failed"
  | "disabled";

export interface AgentInfo {
  kind: "opencode" | "qoder";
  display_name: string;
  installed: boolean;
  path: string;
  version: string;
  install_status: AgentPhase;
  install_stage: string;
  install_source: string;
  configuration_status: string;
  auth_status: "unknown" | "authenticated" | "unauthenticated" | string;
  availability: "available" | "limited" | "unavailable" | "disabled";
  os_supported: boolean;
  enabled: boolean;
  update_policy: string;
  auto_update: boolean;
  last_error: string | null;
  metadata: {
    capabilities?: Record<string, unknown>;
    models?: string[];
    model_count?: number;
    functional_check?: {
      attempted?: boolean;
      verified?: boolean;
      auth_required?: boolean;
      model?: string | null;
      detail?: string;
      duration_ms?: number;
    };
    auth_detail?: string;
    /** False when the CLI's own docs and a real run prove it works signed out. */
    auth_required_for_use?: boolean;
    last_stage_message?: string;
    log_path?: string;
    install_duration_ms?: number;
    [key: string]: unknown;
  };
  log_path: string;
  last_install_at: string | null;
  detected_at: string | null;
  verified_at: string | null;
  auth_checked_at: string | null;
  updated_at: string | null;
}

export interface SetupStatus {
  state: "needs_setup" | "setting_up" | "ready" | "degraded" | "failed";
  agents: Record<string, AgentInfo>;
  ready: string[];
  failed: string[];
  pending: string[];
  unverified: string[];
  auth_needed: string[];
  auth_optional: string[];
  available: string[];
  limited: string[];
  degraded: boolean;
  complete: boolean;
  missing_capabilities: Array<{ kind: string; reason: string }>;
  first_run_complete: boolean;
  auto_install: boolean;
  running: boolean;
  started_at: string | null;
  finished_at: string | null;
  step: string | null;
}

export interface MetricsSnapshot {
  timestamp: string;
  cpu_percent: number;
  ram_percent: number;
  ram_used_bytes: number;
  ram_total_bytes: number;
  disk_percent: number;
  disk_used_bytes: number;
  disk_total_bytes: number;
  disk_free_bytes: number;
  rx_kbps: number;
  tx_kbps: number;
  cpu_count: number;
  history: MetricsSnapshot[];
  host: Record<string, unknown>;
  process: { pid: number; uptime_seconds: number };
}

export interface Project {
  id: string;
  name: string;
  description: string;
  mode: "normal" | "hackathon";
  status: string;
  progress: number;
  workspace_path: string;
  tasks_total: number;
  tasks_completed: number;
  tasks_running: number;
  created_at: string;
  updated_at: string;
}

export interface Worker {
  id: string;
  name: string;
  os_family: string;
  os_version: string;
  arch: string;
  status: string;
  enabled: boolean;
  last_heartbeat_at: string | null;
  capabilities: Record<string, unknown>;
  inventory: Record<string, unknown>;
  current_task_id: string | null;
}

export interface BrowserAccount {
  provider?: string;
  provider_id?: string;
  profile_id?: string;
  status?: string;
  [key: string]: unknown;
}

export interface BrowserProfile {
  id: string;
  name?: string;
  provider?: string;
  status?: string;
  [key: string]: unknown;
}

export interface BrowserHealth {
  connected: boolean;
  error: string | null;
  url: string;
  token_present: boolean;
  service?: Record<string, unknown>;
  status?: Record<string, unknown>;
}

export interface ServerStatus {
  service: string;
  version: string;
  started_at: string | null;
  host: Record<string, unknown>;
  metrics: MetricsSnapshot;
  agents: AgentInfo[];
  setup: SetupStatus;
  browser: BrowserHealth;
  counts: {
    projects: number;
    tasks: number;
    tasks_running: number;
    workers: number;
    workers_online: number;
    human_actions_pending: number;
  };
  workers: Worker[];
}

export interface EventRecord {
  id: number;
  type: string;
  data: Record<string, unknown>;
  timestamp: string;
  important: boolean;
}

export interface DocumentRecord {
  id: string;
  project_id: string | null;
  filename: string;
  mime: string;
  size: number;
  status: string;
  extracted: {
    kind?: string;
    pages?: number | null;
    chars?: number;
    fields?: Record<string, unknown>;
    [key: string]: unknown;
  };
  error: string | null;
  created_at: string;
  text?: string;
  statements?: Array<{ [key: string]: unknown }>;
}

export interface TaskRecord {
  id: string;
  title: string;
  status: string;
  worker_name: string;
  agent_kind: string;
  progress: number;
  error?: string | null;
  project_id?: string;
  description?: string;
  prompt?: string;
  attempts?: number;
  output?: string;
  acceptance?: string[];
  dependencies?: string[];
  files?: string[];
  model_id?: string | null;
  worker_id?: string | null;
  priority?: number;
  allocation_reason?: string;
  risk?: string;
  workspace?: { path: string; branch: string; status: string } | null;
}

export interface CouncilSeat {
  account_id: string;
  label: string;
  email: string;
  council_role: string;
  profile_id: string;
  status: string;
  active: boolean;
  source: string;
  error: string | null;
  last_verified_at: string | null;
}

export interface CouncilProvider {
  provider_id: string;
  display_name: string;
  status: string;
  profile_id: string | null;
  account_id: string;
  account_label: string;
  council_role: string;
  reason: string | null;
  seats: CouncilSeat[];
  accounts: Array<{ status: string | null; profile_id: string | null; error: string | null }>;
}

export interface CouncilRoster {
  providers: CouncilProvider[];
  available: CouncilProvider[];
  connected: boolean;
  error: string | null;
  checked_at: string;
  expected: string[];
}

export interface CouncilMeetingSummary {
  id: string;
  project_id: string | null;
  problem_id: string | null;
  mode: string;
  agenda: string;
  status: string;
  current_round: number;
  rounds_total: number;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
}

export interface CouncilSummary {
  by_status: Record<string, number>;
  running: number;
  last: CouncilMeetingSummary | null;
}

export interface CouncilParticipant {
  provider_id: string;
  profile_id: string;
  account_id: string;
  account_label: string;
  council_role: string;
  status: string;
  included: boolean;
  last_error: string | null;
  last_response_at: string | null;
}

export interface CouncilMessage {
  id: number;
  round: number;
  provider_id: string;
  account_id: string;
  profile_id: string;
  role: string;
  kind: string;
  content: string;
  structured: Record<string, unknown>;
  error: string | null;
  created_at: string;
}

export interface CouncilProposal {
  id: string;
  meeting_id: string;
  author: string;
  account_id: string;
  account_label: string;
  title: string;
  description: string;
  reasoning: string;
  evidence: string[];
  feasibility: number | null;
  confidence: number | null;
  risks: string[];
  responses: Array<Record<string, unknown>>;
  status: string;
  created_at: string;
}

export interface CouncilDisagreement {
  proposal?: string;
  claim?: string;
  counter_claim?: string;
  evidence?: string[];
  severity?: string;
  raised_by?: string;
  [key: string]: unknown;
}

export interface CouncilBrainAttempt {
  kind: string;
  ok: boolean;
  duration_ms: number;
  detail: string | null;
  model: string | null;
}

export interface CouncilBrain {
  order?: string[];
  brief?: string | null;
  brief_by?: string | null;
  brief_error?: string | null;
  brief_attempts?: CouncilBrainAttempt[];
  polished?: string | null;
  polished_by?: string | null;
  polish_error?: string | null;
  polish_attempts?: CouncilBrainAttempt[];
  polished_at?: string | null;
}

export interface CouncilDecision {
  id: string;
  meeting_id: string;
  summary: string;
  rationale: string;
  disagreements: Array<CouncilDisagreement | string>;
  scores: Record<string, unknown>;
  created_at: string;
}

export interface CouncilProblem {
  id: string;
  title: string;
  description: string;
  source_document_id: string | null;
  structured: Record<string, unknown>;
  status: string;
  notes: string;
  created_at: string;
  updated_at: string;
}

export interface CouncilMeeting extends CouncilMeetingSummary {
  instructions: string;
  context: Record<string, unknown>;
  summary: string;
  running: boolean;
  problem: CouncilProblem | null;
  participants: CouncilParticipant[];
  messages: CouncilMessage[];
  proposals: CouncilProposal[];
  decision: CouncilDecision | null;
}

export interface ApprovalRecord {
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

export interface HumanActionRecord {
  id: string;
  title: string;
  detail: string;
  status: string;
  required: boolean;
  created_at: string;
  resolved_at: string | null;
  resolution: string;
}

export interface ProjectSecretRecord {
  name: string;
  updated_at: string | null;
}

export interface ProjectDetail extends Project {
  blueprint: Record<string, unknown> | null;
  ui_spec: Record<string, unknown> | null;
  approvals: ApprovalRecord[];
  human_actions: HumanActionRecord[];
  secrets: ProjectSecretRecord[];
}

export interface WorkspaceStatus {
  is_repo: boolean;
  path: string;
  branch: string | null;
  head?: string | null;
  clean: boolean | null;
  changes: string[];
  upstream?: { ahead: number; behind: number } | null;
  error?: string;
}

export interface GitEntry {
  commit: string;
  author: string;
  date: string;
  subject: string;
}

export interface DeliveryRun {
  id: string;
  project_id: string;
  task_id: string | null;
  command: string;
  status: string;
  duration_ms: number | null;
  created_at: string;
  output_excerpt: string;
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

export interface SchedulerSnapshot {
  by_status: Record<string, number>;
  queue_loop: boolean;
  active_runs: number;
  max_concurrency: number;
}

export interface PairingCode {
  code: string;
  expires_at: string;
  label: string;
  worker_join_url: string;
  ttl_seconds: number;
}

export interface WorkerSnapshot {
  workers: Array<Worker & {
    revoked: boolean;
    connected: boolean;
    paired_at: string | null;
    revoked_at: string | null;
    heartbeat_age_seconds: number | null;
    agents: Array<{
      kind: string;
      path: string;
      version: string;
      installed: boolean;
      usable: boolean;
      detected_at: string | null;
    }>;
    model_count: number;
  }>;
  counts: { workers: number; online: number; revoked: number };
  pairing: {
    active_codes: Array<{ label: string; expires_at: string }>;
    ttl_seconds: number;
  };
  protocol_version: number;
}

export interface TunnelStatus {
  installed: boolean;
  path: string;
  version: string;
  state: "not_installed" | "needs_authtoken" | "stopped" | "starting" | "running" | "error";
  running: boolean;
  public_url: string | null;
  worker_server_url: string;
  local_url: string;
  port: number;
  authtoken_saved: boolean;
  authtoken_source: "forgemesh" | "ngrok" | "";
  log_path: string;
  last_error: string;
  started_at: string | null;
  join_steps: string[];
  worker_install_command: string;
  free_plan_note: string;
  install_supported: boolean;
  install_url: string;
  install_commands: string[];
  saved?: boolean;
  restart_required?: boolean;
  installed_path?: string;
}

export interface DesktopAutostart {
  enabled: boolean;
  path: string;
  error: string;
}

export interface DesktopInfo {
  version: string;
  electron: string;
  platform: string;
  serverMode: string;
  baseUrl: string;
  tokenPath: string;
  logPath: string;
  packaged: boolean;
  autostart: DesktopAutostart;
  windowVisible: boolean;
}

export interface DesktopBridge {
  info(): Promise<DesktopInfo>;
  autostart(): Promise<DesktopAutostart>;
  setAutostart(enabled: boolean): Promise<DesktopAutostart>;
  openLog(): Promise<string>;
  hide(): Promise<boolean>;
  restart(): Promise<{ ok: boolean; error: string }>;
  retry(): void;
  quit(): void;
  onStartupState(callback: (state: unknown) => void): () => void;
  onStartupLog(callback: (text: string) => void): () => void;
  onServerRestarting(callback: () => void): () => void;
}

declare global {
  interface Window {
    forgemeshDesktop?: DesktopBridge;
  }
}

/* ------------------------------------------------------------------ coding agents */

export interface AgentCapability {
  interactive_login: boolean;
  provider_selection: boolean;
  logout: boolean;
  multiple_accounts: boolean;
  account_isolation: string;
  /** False when the tool runs real prompts without any stored credential. */
  required_for_basic_use: boolean;
  limitation: string;
  kind: string;
  installed: boolean;
  terminal: string;
  terminal_note: string;
  auth_status: string;
  availability: string;
}

export interface AgentCliAccount {
  id: number;
  kind: string;
  name: string;
  status: string;
  detail: string;
  active: boolean;
  switchable: boolean;
  config_dir: string;
  source: string;
  last_verified_at: string | null;
  created_at: string | null;
}

export interface AgentAuthSessionRecord {
  id: string;
  kind: string;
  command: string;
  status: string;
  exit_code: number | null;
  detail: string;
  tail: string;
  started_at: string | null;
  ended_at: string | null;
}

export interface AgentRunningSession {
  id: string;
  kind: string;
  command: string;
  mode: string;
  status: string;
  exit_code: number | null;
  next_offset: number;
  started_at: string | null;
  ended_at: string | null;
}

export interface AgentAuthState {
  kind: string;
  capability: AgentCapability;
  accounts: AgentCliAccount[];
  signed_in: number;
  active_account: string | null;
  running_session: AgentRunningSession | null;
  history: AgentAuthSessionRecord[];
}

export interface AgentTerminalView {
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
  started_at: string | null;
  ended_at: string | null;
  timeout_seconds: number;
}

export interface AgentModelList {
  kind: string;
  models: string[];
  auth_status?: string;
  account?: string | null;
  error?: string;
}

export interface AgentLogText {
  kind: string;
  path: string;
  log: string;
  stored_tail: string;
}
