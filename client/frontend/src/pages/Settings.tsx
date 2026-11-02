import { useEffect, useState } from "react";
import { api, shell } from "../api";
import { useApp } from "../context";
import { PairingForm } from "../components/PairingForm";
import {
  Button,
  Icon,
  Pill,
  SectionHeader,
  formatBytes,
  formatDate,
  timeAgo
} from "../components/ui";

export default function Settings() {
  const { snapshot, refresh, busy, setBusy, toast } = useApp();
  const { connection, worker, security, app, operator } = snapshot;
  const [operatorToken, setOperatorToken] = useState("");
  const [showOperator, setShowOperator] = useState(false);
  const [takeover, setTakeover] = useState(false);
  const [stopUnit, setStopUnit] = useState(false);
  const [allowInsecure, setAllowInsecure] = useState(false);
  const [insecurePhrase, setInsecurePhrase] = useState("");
  const [autostart, setAutostart] = useState<{ enabled: boolean; path: string } | null>(null);
  const [disk, setDisk] = useState<{ total_bytes: number; used_bytes: number; free_bytes: number; percent: number | null; path: string } | null>(null);
  const [update, setUpdate] = useState<{ ok?: boolean; current?: string; latest?: string; update_available?: boolean; url?: string; error?: string } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    void shell.autostart().then(setAutostart).catch(() => setAutostart(null));
    void api.disk().then(setDisk).catch(() => setDisk(null));
  }, []);

  const run = async (name: string, action: () => Promise<unknown>, message: string, tone: "ok" | "warn" | "bad" = "ok") => {
    setBusy(name);
    setError("");
    try {
      await action();
      toast(message, tone);
      await refresh();
    } catch (err: unknown) {
      setError((err as Error).message);
      toast((err as Error).message, "bad");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="page settings">
      <SectionHeader title="Connection" subtitle="Pairing identity, token storage and transport security." />

      {error && <div className="inline-alert bad"><Icon name="alert" size={17} /><span>{error}</span></div>}

      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <div><h2>Device pairing</h2><p>Single-use server codes; local identity never exposes the token.</p></div>
            <Pill tone={connection.paired ? "green" : "amber"}>{connection.paired ? "paired" : "unpaired"}</Pill>
          </div>
          <PairingForm />
        </section>

        <div className="stack">
          <section className="card">
            <div className="card-head"><div><h2>Token storage</h2><p>Where the worker credential actually lives.</p></div></div>
            <dl className="kv">
              <div><dt>Backend</dt><dd>{security.secret_backend === "secret_service" ? "Secret Service (secret-tool)" : security.secret_backend === "file" ? "0600 file fallback" : "not stored"}</dd></div>
              <div><dt>Fingerprint</dt><dd className="mono">{connection.token_fingerprint || "—"}</dd></div>
              <div><dt>secret-tool present</dt><dd>{security.secret_tool_available ? "yes" : "no"}</dd></div>
              <div><dt>Worker token present</dt><dd>{security.has_worker_token ? "yes" : "no"}</dd></div>
            </dl>
            {connection.token_warning && (
              <div className="inline-alert warn">
                <Icon name="shield" size={17} />
                <span>{connection.token_warning}</span>
              </div>
            )}
            <p className="small muted">
              The fingerprint is the first four hex characters of the token's SHA-256, shown so two
              people can compare stores without reading the credential.
            </p>
          </section>

          <section className="card">
            <div className="card-head"><div><h2>Transport security</h2><p>Plain HTTP is refused unless explicitly acknowledged.</p></div></div>
            <dl className="kv">
              <div><dt>Server URL</dt><dd className="mono ellipsis">{connection.server_url || "—"}</dd></div>
              <div><dt>Current mode</dt><dd>{connection.insecure_http ? <span className="bad">plain HTTP allowed</span> : "HTTPS or loopback"}</dd></div>
            </dl>
            {!connection.insecure_http ? (
              <details className="danger-zone">
                <summary><Icon name="alert" size={16} /> Allow plain HTTP to a non-loopback server</summary>
                <p>This sends the worker token and task output in clear text on the network. It is never the default and the warning stays visible.</p>
                <label className="check">
                  <input type="checkbox" checked={allowInsecure} onChange={(event) => setAllowInsecure(event.target.checked)} />
                  <span>I understand the risk</span>
                </label>
                <div className="field">
                  <label>Type ALLOW PLAIN HTTP</label>
                  <input value={insecurePhrase} onChange={(event) => setInsecurePhrase(event.target.value)} placeholder="ALLOW PLAIN HTTP" />
                </div>
                <Button
                  variant="danger"
                  icon="shield"
                  disabled={!allowInsecure || insecurePhrase.trim() !== "ALLOW PLAIN HTTP" || busy === "insecure"}
                  onClick={() => run("insecure", () => api.config({ insecure_http: true, insecure_confirmation: insecurePhrase }), "Plain HTTP enabled; the persistent warning is now visible.", "warn")}
                >
                  Enable plain HTTP
                </Button>
              </details>
            ) : (
              <Button
                variant="secondary"
                icon="shield"
                disabled={busy === "secure"}
                onClick={() => run("secure", () => api.config({ insecure_http: false }), "Plain HTTP disabled.")}
              >
                Disable plain HTTP
              </Button>
            )}
          </section>
        </div>
      </div>

      <SectionHeader title="Worker supervision" subtitle="Choose who owns the worker loop: this app or the optional systemd unit." />

      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <div><h2>Process control</h2><p>Stopping the GUI-owned worker leaves the server row online until it times out.</p></div>
            <Pill tone={worker.pid ? "green" : worker.unit_active ? "violet" : "neutral"}>
              {worker.pid ? `PID ${worker.pid}` : worker.unit_active ? "systemd" : "stopped"}
            </Pill>
          </div>
          <dl className="kv">
            <div><dt>Status</dt><dd>{worker.status}</dd></div>
            <div><dt>Owner</dt><dd>{worker.owner}</dd></div>
            <div><dt>Started</dt><dd>{formatDate(worker.started_at)}</dd></div>
            <div><dt>Last heartbeat</dt><dd>{formatDate(worker.last_heartbeat_at)}</dd></div>
            <div><dt>Unit active</dt><dd>{worker.unit_active ? "yes" : "no"}</dd></div>
          </dl>
          <label className="check">
            <input type="checkbox" checked={takeover} onChange={(event) => setTakeover(event.target.checked)} />
            <span>Take over from <span className="mono">forgemesh-worker.service</span> before starting</span>
          </label>
          <div className="row wrap">
            <Button size="sm" icon="play" disabled={Boolean(worker.pid) || busy === "start"} onClick={() => run("start", () => api.startWorker(takeover), "Worker started.", "ok")}>Start</Button>
            <Button size="sm" variant="secondary" icon="refresh" disabled={!connection.paired || busy === "restart"} onClick={() => run("restart", () => api.restartWorker(takeover), "Worker restarted.", "warn")}>Restart</Button>
            <Button size="sm" variant="danger" icon="stop" disabled={!worker.pid || busy === "stop"} onClick={() => run("stop", () => api.stopWorker(stopUnit), "Worker stopped.", "warn")}>Stop</Button>
          </div>
          <label className="check">
            <input type="checkbox" checked={stopUnit} onChange={(event) => setStopUnit(event.target.checked)} />
            <span>When stopping, also stop the systemd unit</span>
          </label>
          <p className="small muted">
            Reconnect attempts reported by the child: {worker.reconnect ? `attempt ${worker.reconnect.attempt} in ${worker.reconnect.delay_seconds}s` : "none"}.
          </p>
        </section>

        <section className="card">
          <div className="card-head">
            <div><h2>Operator mode</h2><p>Optional server control token for P1 review screens.</p></div>
            <Pill tone={operator.available ? "green" : "neutral"}>{operator.available ? "enabled" : "off"}</Pill>
          </div>
          <p className="small muted">
            The worker token is scoped to the worker socket. Projects, approvals, tasks and delivery
            routes require the server control token. Operator mode stores it separately at{" "}
            <span className="mono">0600</span> and never hands it to the worker child.
          </p>
          {!operator.available ? (
            <>
              <div className="row wrap">
                <Button size="sm" icon="shield" disabled={busy === "operator-local"} onClick={() => run("operator-local", () => api.enableOperatorLocal(), "Operator mode enabled from the local control token.")}>
                  Use local control token
                </Button>
                <Button size="sm" variant="secondary" icon="terminal" onClick={() => setShowOperator((value) => !value)}>
                  Paste a token
                </Button>
              </div>
              {showOperator && (
                <div className="field">
                  <label>Server control token</label>
                  <input type="password" value={operatorToken} onChange={(event) => setOperatorToken(event.target.value)} placeholder="control token" autoComplete="off" />
                  <small>The token is written to a private file and is only used by the daemon's server requests.</small>
                  <Button size="sm" disabled={!operatorToken.trim() || busy === "operator"} onClick={() => run("operator", () => api.enableOperator(operatorToken), "Operator mode enabled; server counts refreshed.")}>
                    Enable
                  </Button>
                </div>
              )}
            </>
          ) : (
            <>
              <div className="mini-stats">
                <div><strong>{operator.counts.projects ?? 0}</strong><span>projects</span></div>
                <div><strong>{operator.counts.tasks ?? 0}</strong><span>tasks</span></div>
                <div><strong>{operator.counts.workers ?? 0}</strong><span>workers</span></div>
                <div><strong>{operator.counts.documents ?? 0}</strong><span>docs</span></div>
              </div>
              <dl className="kv">
                <div><dt>Token source</dt><dd>{connection.operator_token_source || "—"}</dd></div>
                <div><dt>Last refresh</dt><dd>{operator.last_refresh ? timeAgo(operator.last_refresh) : "—"}</dd></div>
                <div><dt>Refresh error</dt><dd className={operator.error ? "bad" : ""}>{operator.error || "none"}</dd></div>
              </dl>
              <div className="row">
                <Button size="sm" variant="secondary" icon="refresh" onClick={() => run("operator-refresh", async () => { await api.enableOperatorLocal(); }, "Operator data refreshed.")}>Refresh data</Button>
                <Button size="sm" variant="danger" icon="power" onClick={() => run("operator-off", () => api.disableOperator(), "Operator mode disabled; token removed.", "warn")}>Disable</Button>
              </div>
            </>
          )}
        </section>
      </div>

      <SectionHeader title="Desktop and storage" subtitle="Autostart, disk usage, self-update check and file locations." />

      <div className="grid-2">
        <section className="card">
          <div className="card-head"><div><h2>Desktop integration</h2><p>Autostart writes only a user .desktop entry.</p></div></div>
          <div className="row spread">
            <div>
              <strong>Start with the desktop session</strong>
              <p className="small muted mono ellipsis">{autostart?.path || "Unavailable outside Electron."}</p>
            </div>
            <Button
              size="sm"
              variant={autostart?.enabled ? "secondary" : "primary"}
              icon={autostart?.enabled ? "close" : "check"}
              disabled={busy === "autostart"}
              onClick={async () => {
                setBusy("autostart");
                try {
                  const next = await shell.setAutostart(!(autostart?.enabled ?? false));
                  setAutostart(next);
                  toast(next.enabled ? "Autostart enabled." : "Autostart disabled.", "ok");
                } catch (err: unknown) {
                  toast((err as Error).message, "bad");
                } finally {
                  setBusy("");
                }
              }}
            >
              {autostart?.enabled ? "Disable" : "Enable"}
            </Button>
          </div>
          <div className="row spread top-gap">
            <div>
              <strong>Daemon log</strong>
              <p className="small muted mono ellipsis">{app.paths.logs}</p>
            </div>
            <Button size="sm" variant="secondary" icon="external" onClick={() => void shell.openLog()}>Open</Button>
          </div>
        </section>

        <section className="card">
          <div className="card-head"><div><h2>Disk usage</h2><p>Measured by the daemon on its real data directory.</p></div></div>
          {disk ? (
            <>
              <div className="disk-bar">
                <i style={{ width: `${Math.min(100, disk.percent ?? 0)}%` }} />
              </div>
              <dl className="kv">
                <div><dt>Path</dt><dd className="mono ellipsis">{disk.path}</dd></div>
                <div><dt>Used</dt><dd>{formatBytes(disk.used_bytes)} of {formatBytes(disk.total_bytes)}</dd></div>
                <div><dt>Free</dt><dd>{formatBytes(disk.free_bytes)}</dd></div>
                <div><dt>Percent</dt><dd>{disk.percent != null ? `${disk.percent}%` : "—"}</dd></div>
              </dl>
            </>
          ) : (
            <p className="small muted">Disk statistics unavailable.</p>
          )}
          <div className="row top-gap">
            <Button size="sm" variant="secondary" icon="download" disabled={busy === "update"} onClick={async () => {
              setBusy("update");
              try {
                const result = (await api.updateCheck()) as typeof update;
                setUpdate(result);
                if (!result?.ok) toast(result?.error || "Update check failed.", "warn");
                else toast(result.update_available ? `Version ${result.latest} is available.` : "You are current.", result.update_available ? "warn" : "ok");
              } catch (err: unknown) {
                toast((err as Error).message, "bad");
              } finally {
                setBusy("");
              }
            }}>
              {busy === "update" ? "Checking…" : "Check for updates"}
            </Button>
            {update && (
              <span className="small muted">
                {update.ok
                  ? update.update_available
                    ? `latest ${update.latest} · current ${update.current}`
                    : `current ${update.current} is the latest release`
                  : update.error}
              </span>
            )}
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card-head"><div><h2>Local paths</h2><p>All directories are created with private permissions.</p></div></div>
        <dl className="kv wide">
          {Object.entries(app.paths).map(([name, value]) => (
            <div key={name}><dt>{name}</dt><dd className="mono ellipsis">{value}</dd></div>
          ))}
        </dl>
      </section>
    </div>
  );
}
