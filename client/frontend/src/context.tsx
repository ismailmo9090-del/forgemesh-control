import { createContext, useContext } from "react";
import type { Snapshot } from "./types";

export type PageId =
  | "dashboard"
  | "agents"
  | "tasks"
  | "review"
  | "approvals"
  | "activity"
  | "settings"
  | "about";

export interface WizardRequest {
  kind: string;
  operation: "install" | "update" | "verify";
}

export interface AuthRequest {
  kind: string;
  sessionId?: string;
}

export interface AppContextValue {
  snapshot: Snapshot;
  refresh: () => Promise<void>;
  busy: string;
  setBusy: (value: string) => void;
  toast: (message: string, tone?: "ok" | "warn" | "bad") => void;
  navigate: (page: PageId) => void;
  openWizard: (request: WizardRequest) => void;
  openAuth: (request: AuthRequest) => void;
  openPairing: () => void;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const value = useContext(AppContext);
  if (!value) throw new Error("useApp must be used inside AppContext");
  return value;
}
