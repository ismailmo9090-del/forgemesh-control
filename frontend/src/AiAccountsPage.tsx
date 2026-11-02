import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError, formatTime } from "./api";
import { Card, Empty, PROVIDER_META, StatusChip } from "./components/ui";

/**
 * Every status here is the browser runtime's own verdict, re-read from the
 * server. Nothing in this page marks an account signed in before the runtime
 * says so, and no field it shows can carry a cookie or token.
 */

export interface AiAccount {
  id: string;
  provider_id: string;
  label: string;
  email: string;
  profile_id: string;
  council_role: string;
  active: boolean;
  enabled: boolean;
  status: string;
  verified: boolean;
  last_login_at: string | null;
  last_verified_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AiProviderCard {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  login_supported: boolean;
  managed_by_browser: boolean;
  council: boolean;
  accounts: AiAccount[];
  configured: number;
  authenticated: number;
  needs_attention: number;
  active_account: AiAccount | null;
  last_verified_at: string | null;
  profiles_present: boolean | null;
}

export interface AiAccountSummary {
  connected: boolean;
  error: string | null;
  providers: AiProviderCard[];
  totals: { accounts: number; authenticated: number; providers_with_accounts: number };
}

const STATUS_LABEL: Record<string, string> = {
  available: "Signed in",
  needs_reauth: "Needs re-auth",
  login_pending: "Login window open",
  rate_limited: "Rate limited",
  error: "Error",
  unverified: "Not verified"
};

const STATUS_TONE: Record<string, "ok" | "warn" | "bad" | "muted" | "info"> = {
  available: "ok",
  needs_reauth: "warn",
  login_pending: "info",
  rate_limited: "warn",
  error: "bad",
  unverified: "muted"
};

function Chip({ status }: { status: string }) {
  return (
    <StatusChip
      status={status}
      label={STATUS_LABEL[status] || status.replace(/_/g, " ")}
      tone={STATUS_TONE[status] || "muted"}
    />
  );
}

function ActionButton({
  label,
  working,
  disabled,
  title,
  primary,
  onClick
}: {
  label: string;
  working: boolean;
  disabled?: boolean;
  title?: string;
  primary?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className={`btn small-btn${primary ? " primary" : ""}`}
      onClick={onClick}
      disabled={disabled || working}
      title={title}
    >
      {working ? `${label}…` : label}
    </button>
  );
}

export default function AiAccountsPage() {
  const [summary, setSummary] = useState<AiAccountSummary | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<Record<string, string>>({});
  const [addingFor, setAddingFor] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; label: string } | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      setSummary(await api<AiAccountSummary>("/api/accounts"));
      setLoadError("");
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : String(error));
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, [load]);

  const say = (key: string, message: string) => {
    setNotice((previous) => ({ ...previous, [key]: message }));
    if (message) window.setTimeout(() => setNotice((previous) => ({ ...previous, [key]: "" })), 12000);
  };

  const run = async (
    key: string,
    action: string,
    request: () => Promise<unknown>,
    onDone?: (value: any) => void
  ) => {
    setBusy((previous) => ({ ...previous, [key]: action }));
    say(key, "");
    try {
      const value = await request();
      onDone?.(value);
    } catch (error) {
      say(key, error instanceof ApiError ? error.message : String(error));
    } finally {
      setBusy((previous) => {
        const next = { ...previous };
        delete next[key];
        return next;
      });
    }
    await load();
  };

  const providers = summary?.providers || [];

  return (
    <div className="page-stack">
      <Card
        title="AI provider accounts"
        subtitle="One isolated ForgeMesh Browser profile per account. Signing in always happens in that browser window, never in this dashboard."
        action={
          <button className="btn small-btn" onClick={() => void load()}>
            Refresh
          </button>
        }
        live
      >
        {summary && (
          <div className="detail-grid">
            <div>
              <span>Accounts registered</span>
              <strong>{String(summary.totals.accounts)}</strong>
            </div>
            <div>
              <span>Verified sessions</span>
              <strong>{String(summary.totals.authenticated)}</strong>
            </div>
            <div>
              <span>Providers in use</span>
              <strong>
                {String(summary.totals.providers_with_accounts)}/{String(summary.providers.length)}
              </strong>
            </div>
            <div>
              <span>Browser runtime</span>
              <StatusChip
                status={summary.connected ? "online" : "offline"}
                label={summary.connected ? "reachable" : "unreachable"}
              />
            </div>
          </div>
        )}
        {loadError && <p className="error-box">{loadError}</p>}
        {summary && !summary.connected && (
          <p className="warn-box">
            ForgeMesh Browser is not answering{summary.error ? `: ${summary.error}` : ""}. The
            registry can still be read, but no login window, verification or logout can run until it
            does.
          </p>
        )}
        {!summary && !loadError && (
          <Empty
            title="Reading the account registry"
            detail="Asking the control server for the provider list."
          />
        )}
        <p className="info-box">
          Credentials, cookies, tokens and one-time codes are never typed or stored here. The browser
          profile keeps its own session; ForgeMesh only records whether that profile is signed in.
        </p>
      </Card>

      {providers.map((provider) => {
        const meta = PROVIDER_META[provider.id] || {
          label: provider.name,
          glyph: "•",
          color: "var(--accent)"
        };
        return (
          <Card
            key={provider.id}
            title={`${provider.name} accounts`}
            subtitle={
              provider.managed_by_browser
                ? "Session state comes from the browser profile behind each account."
                : "Known to the council, but no browser profile exposes it yet."
            }
            action={
              <button
                className="link-btn"
                onClick={() => setAddingFor(addingFor === provider.id ? null : provider.id)}
                disabled={!provider.login_supported}
                title={
                  provider.login_supported ? undefined : "The browser runtime offers no login for this provider"
                }
              >
                {addingFor === provider.id ? "Cancel" : "Add account"}
              </button>
            }
          >
            <div className="provider-facts">
              <span className="roster-glyph" style={{ color: meta.color }} aria-hidden>
                {meta.glyph}
              </span>
              <div>
                <strong>
                  {provider.configured} account{provider.configured === 1 ? "" : "s"} ·{" "}
                  {provider.authenticated} signed in
                </strong>
                <small>
                  {provider.url ? (
                    <a href={provider.url} target="_blank" rel="noreferrer">
                      {provider.url}
                    </a>
                  ) : (
                    "no sign-in url published by the browser runtime"
                  )}
                  {" · "}
                  {provider.active_account
                    ? `active: ${provider.active_account.label}`
                    : "no active account"}
                  {" · last verified "}
                  {formatTime(provider.last_verified_at)}
                </small>
              </div>
              {provider.needs_attention > 0 && (
                <StatusChip
                  status="warn"
                  label={`${provider.needs_attention} need attention`}
                  tone="warn"
                />
              )}
              {provider.profiles_present === false && (
                <StatusChip status="bad" label="profile missing in browser" tone="bad" />
              )}
            </div>

            {addingFor === provider.id && (
              <AddAccountForm
                providerId={provider.id}
                onCancel={() => setAddingFor(null)}
                onCreated={(account) =>
                  say(
                    `provider:${provider.id}`,
                    `${account.label} registered with profile ${account.profile_id}. Open its login window to sign in.`
                  )
                }
              />
            )}
            {notice[`provider:${provider.id}`] && (
              <p className="info-box">{notice[`provider:${provider.id}`]}</p>
            )}

            {provider.accounts.length === 0 ? (
              <Empty
                title={`No ${provider.name} account yet`}
                detail="Add one to reserve a dedicated browser profile. Signing in still needs you to type the credentials in that profile's own window."
              />
            ) : (
              <div className="account-table">
                <div className="account-row head">
                  <span>Account</span>
                  <span>Session</span>
                  <span>Timings</span>
                  <span>Actions</span>
                </div>
                {provider.accounts.map((account) => {
                  const key = `acct:${account.id}`;
                  const working = busy[key] || "";
                  return (
                    <div key={account.id} className={`account-row${account.active ? " is-active" : ""}`}>
                      <div className="account-id">
                        {renaming?.id === account.id ? (
                          <div className="rename-inline">
                            <input
                              value={renaming.label}
                              autoFocus
                              maxLength={120}
                              onChange={(event) =>
                                setRenaming({ id: account.id, label: event.target.value })
                              }
                              onKeyDown={(event) => {
                                if (event.key === "Enter") {
                                  const next = renaming.label.trim();
                                  setRenaming(null);
                                  if (next && next !== account.label) {
                                    void run(key, "rename", () =>
                                      api(`/api/accounts/${account.id}`, {
                                        method: "PATCH",
                                        body: JSON.stringify({ label: next })
                                      })
                                    );
                                  }
                                }
                                if (event.key === "Escape") setRenaming(null);
                              }}
                            />
                            <button
                              className="btn small-btn primary"
                              onClick={() => {
                                const next = renaming.label.trim();
                                setRenaming(null);
                                if (next && next !== account.label) {
                                  void run(key, "rename", () =>
                                    api(`/api/accounts/${account.id}`, {
                                      method: "PATCH",
                                      body: JSON.stringify({ label: next })
                                    })
                                  );
                                }
                              }}
                            >
                              Save name
                            </button>
                          </div>
                        ) : (
                          <>
                            <strong>
                              {account.label}
                              {account.active && <em className="tag-active">active</em>}
                              {!account.enabled && <em className="tag-off">disabled</em>}
                            </strong>
                            <small className="mono">
                              profile {account.profile_id}
                              {account.council_role ? ` · role ${account.council_role}` : ""}
                              {account.email ? ` · ${account.email}` : ""}
                            </small>
                            {account.last_error && <small className="warn-text">{account.last_error}</small>}
                          </>
                        )}
                      </div>
                      <div className="account-state">
                        <Chip status={account.status} />
                        <small>
                          {account.verified
                            ? "the browser reports a live session"
                            : "not confirmed by the browser"}
                        </small>
                      </div>
                      <div className="account-state">
                        <small>verified {formatTime(account.last_verified_at)}</small>
                        <small>login {formatTime(account.last_login_at)}</small>
                        <small>error {formatTime(account.last_error_at)}</small>
                      </div>
                      <div className="account-actions">
                        <ActionButton
                          label={
                            account.status === "login_pending" ? "Open login window" : "Login"
                          }
                          working={working === "login"}
                          onClick={() =>
                            void run(
                              key,
                              "login",
                              () =>
                                api<{ logged_in?: boolean; url?: string }>(
                                  `/api/accounts/${account.id}/login/begin`,
                                  { method: "POST", body: JSON.stringify({ headless: false }) }
                                ),
                              (result) =>
                                say(
                                  key,
                                  result.logged_in
                                    ? "This profile already held a live session - no login needed."
                                    : `Login window opened at ${result.url || "the provider sign-in page"}. Type your credentials there, complete any OTP, MFA or CAPTCHA yourself, then press “I've completed login”.`
                                )
                            )
                          }
                        />
                        {account.status === "login_pending" && (
                          <ActionButton
                            label="I've completed login"
                            primary
                            working={working === "complete"}
                            onClick={() =>
                              void run(
                                key,
                                "complete",
                                () =>
                                  api<AiAccount>(`/api/accounts/${account.id}/login/complete`, {
                                    method: "POST"
                                  }),
                                (verified: AiAccount) =>
                                  say(
                                    key,
                                    verified.verified
                                      ? "Verified: this browser profile holds a real signed-in session."
                                      : `Still signed out (status ${verified.status}).${
                                          verified.last_error ? ` ${verified.last_error}` : ""
                                        }`
                                  )
                              )
                            }
                          />
                        )}
                        <ActionButton
                          label="Verify session"
                          working={working === "verify"}
                          onClick={() =>
                            void run(
                              key,
                              "verify",
                              () =>
                                api<AiAccount>(`/api/accounts/${account.id}/verify`, {
                                  method: "POST"
                                }),
                              (checked: AiAccount) =>
                                say(
                                  key,
                                  checked.verified
                                    ? `Session verified at ${formatTime(checked.last_verified_at)}.`
                                    : `Not usable: ${
                                        STATUS_LABEL[checked.status] || checked.status
                                      }${checked.last_error ? ` — ${checked.last_error}` : ""}`
                                )
                            )
                          }
                        />
                        <ActionButton
                          label="Set active"
                          working={working === "active"}
                          disabled={!account.verified || account.active}
                          title={
                            account.verified
                              ? "The council uses this account for the provider"
                              : "Only a session the browser verified can be made active"
                          }
                          onClick={() =>
                            void run(
                              key,
                              "active",
                              () => api(`/api/accounts/${account.id}/active`, { method: "POST" }),
                              () => say(key, "This account is now the one the council uses.")
                            )
                          }
                        />
                        <ActionButton
                          label="Rename"
                          working={working === "rename"}
                          onClick={() => setRenaming({ id: account.id, label: account.label })}
                        />
                        <ActionButton
                          label={account.enabled ? "Disable" : "Enable"}
                          working={working === "enabled"}
                          onClick={() =>
                            void run(key, "enabled", () =>
                              api(`/api/accounts/${account.id}`, {
                                method: "PATCH",
                                body: JSON.stringify({ enabled: !account.enabled })
                              })
                            )
                          }
                        />
                        <ActionButton
                          label="Logout"
                          working={working === "logout"}
                          disabled={account.status === "unverified"}
                          onClick={() =>
                            void run(
                              key,
                              "logout",
                              () =>
                                api<{ signed_out: boolean; detail: string }>(
                                  `/api/accounts/${account.id}/logout`,
                                  { method: "POST" }
                                ),
                              (result) =>
                                say(
                                  key,
                                  result.signed_out
                                    ? "Signed out - the provider no longer reports a session."
                                    : result.detail || "Sign-out could not be confirmed."
                                )
                            )
                          }
                        />
                        <ActionButton
                          label={confirmingDelete === account.id ? "Confirm remove" : "Remove"}
                          primary={confirmingDelete === account.id}
                          working={working === "delete"}
                          onClick={() => {
                            if (confirmingDelete !== account.id) {
                              setConfirmingDelete(account.id);
                              say(
                                key,
                                "Removing also deletes its browser profile and any session inside it. Press again to confirm."
                              );
                              return;
                            }
                            setConfirmingDelete(null);
                            void run(
                              key,
                              "delete",
                              () =>
                                api<{ profile_deleted: boolean }>(
                                  `/api/accounts/${account.id}/delete`,
                                  { method: "POST", body: JSON.stringify({ confirm: true, purge_profile: true }) }
                                ),
                              (result) =>
                                say(
                                  key,
                                  result.profile_deleted
                                    ? "Account and its browser profile removed."
                                    : "Account removed; the browser kept the profile."
                                )
                            );
                          }}
                        />
                      </div>
                      {notice[key] && <p className="info-box span-row">{notice[key]}</p>}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        );
      })}
    </div>
  );
}

function AddAccountForm({
  providerId,
  onCancel,
  onCreated
}: {
  providerId: string;
  onCancel: () => void;
  onCreated: (account: AiAccount) => void;
}) {
  const [label, setLabel] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  return (
    <form
      className="form-grid"
      onSubmit={async (event) => {
        event.preventDefault();
        setBusy(true);
        setError("");
        try {
          const created = await api<AiAccount>("/api/accounts", {
            method: "POST",
            body: JSON.stringify({ provider_id: providerId, label, email, council_role: role })
          });
          onCreated(created);
          onCancel();
        } catch (caught) {
          setError(caught instanceof ApiError ? caught.message : String(caught));
        } finally {
          setBusy(false);
        }
      }}
    >
      <label className="field">
        <span>Label</span>
        <input
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          placeholder="optional - the server names it if left blank"
          maxLength={120}
        />
        <small>Never a password. This only labels the browser profile.</small>
      </label>
      <label className="field">
        <span>Email (optional)</span>
        <input
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          placeholder="identification only, never a secret"
          maxLength={200}
        />
      </label>
      <label className="field">
        <span>Council role</span>
        <input
          value={role}
          onChange={(event) => setRole(event.target.value)}
          placeholder="e.g. reviewer"
          maxLength={60}
        />
      </label>
      <div className="card-actions">
        <button className="btn" type="submit" disabled={busy}>
          {busy ? "Reserving profile…" : "Add account"}
        </button>
        <button className="btn small-btn" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && <p className="error-box">{error}</p>}
    </form>
  );
}
