import { useMemo, useState } from "react";
import { api } from "../api";
import { useApp, type AppContextValue } from "../context";
import { Button, Field, Icon, Pill, timeAgo } from "./ui";

const LOOPBACK = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

function parseServer(value: string): { host: string; scheme: string; valid: boolean } {
  try {
    const url = new URL(value.trim());
    return { host: url.hostname, scheme: url.protocol.replace(":", ""), valid: Boolean(url.hostname) };
  } catch {
    return { host: "", scheme: "", valid: false };
  }
}

export function PairingForm({ compact = false, onDone }: { compact?: boolean; onDone?: () => void }) {
  const { snapshot, refresh, toast } = useApp() as AppContextValue;
  const connection = snapshot.connection;
  const [server, setServer] = useState(connection.server_url || "http://127.0.0.1:8787");
  const [code, setCode] = useState("");
  const [name, setName] = useState(connection.worker_name || "");
  const [allowInsecure, setAllowInsecure] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const [unpairText, setUnpairText] = useState("");
  const [revokeRemote, setRevokeRemote] = useState(true);

  const parsed = useMemo(() => parseServer(server), [server]);
  const insecureNeeded = parsed.valid && parsed.scheme === "http" && !LOOPBACK.has(parsed.host);

  const pair = async () => {
    setError(null);
    if (!parsed.valid) {
      setError({ code: "invalid_url", message: "Enter a full server URL, for example https://mesh.example.com." });
      return;
    }
    if (insecureNeeded && (!allowInsecure || phrase.trim() !== "ALLOW PLAIN HTTP")) {
      setError({
        code: "confirmation_required",
        message: "Plain HTTP to a non-loopback host sends the worker token in clear text. Tick the box and type ALLOW PLAIN HTTP."
      });
      return;
    }
    setBusy("pair");
    try {
      const next = await api.pair({
        server_url: server.trim(),
        code: code.trim(),
        name: name.trim(),
        insecure_http: insecureNeeded && allowInsecure,
        insecure_confirmation: insecureNeeded ? phrase : ""
      });
      void next;
      setCode("");
      toast("Device paired. The worker is starting.", "ok");
      await refresh();
      onDone?.();
    } catch (err: unknown) {
      const apiError = err as { code?: string; message?: string };
      setError({ code: apiError.code || "pairing_failed", message: apiError.message || String(err) });
    } finally {
      setBusy("");
    }
  };

  const test = async () => {
    setBusy("test");
    try {
      const result = (await api.connectionTest()) as { connection?: { latency_ms?: number; error?: string } };
      const latency = result.connection?.latency_ms;
      toast(latency != null ? `Server answered in ${latency} ms.` : result.connection?.error || "Connection checked.", latency != null ? "ok" : "warn");
      await refresh();
    } catch (err: unknown) {
      toast((err as Error).message, "bad");
    } finally {
      setBusy("");
    }
  };

  const unpair = async () => {
    if (unpairText.trim() !== "UNPAIR") {
      setError({ code: "confirmation_required", message: "Type UNPAIR exactly to remove the local worker token." });
      return;
    }
    setBusy("unpair");
    setError(null);
    try {
      const result = (await api.unpair({ confirm: unpairText, revoke_remote: revokeRemote })) as {
        revoked?: boolean;
      };
      setUnpairText("");
      toast(result.revoked ? "Unpaired and revoked on the server." : "Unpaired locally.", "warn");
      await refresh();
      onDone?.();
    } catch (err: unknown) {
      toast((err as Error).message, "bad");
    } finally {
      setBusy("");
    }
  };

  if (connection.paired) {
    return (
      <div className={compact ? "pair-panel compact" : "pair-panel paired"}>
        <div className="identity-card">
          <div className="identity-head">
            <div>
              <span className="eyebrow">This device</span>
              <h3>{connection.worker_name || "Paired worker"}</h3>
            </div>
            <Pill tone={connection.insecure_http ? "red" : "green"}>
              {connection.insecure_http ? "Plain HTTP" : "Encrypted token path"}
            </Pill>
          </div>
          <dl className="kv">
            <div><dt>Worker ID</dt><dd className="mono">{connection.worker_id || "—"}</dd></div>
            <div><dt>Token fingerprint</dt><dd className="mono">{connection.token_fingerprint || "—"}</dd></div>
            <div><dt>Server</dt><dd className="mono ellipsis">{connection.server_url || "—"}</dd></div>
            <div><dt>Token store</dt><dd>{connection.token_backend === "secret_service" ? "Secret Service" : connection.token_backend === "file" ? "0600 file (fallback)" : connection.token_backend || "—"}</dd></div>
            <div><dt>Last hello</dt><dd>{timeAgo(connection.last_hello_at)}</dd></div>
            <div><dt>Latency</dt><dd>{connection.latency_ms != null ? `${connection.latency_ms} ms` : "—"}</dd></div>
          </dl>
          {connection.token_warning && (
            <div className="pair-warning">
              <Icon name="shield" size={15} />
              <span>{connection.token_warning}</span>
            </div>
          )}
        </div>

        <div className="pair-actions">
          <Button variant="secondary" icon="wifi" onClick={test} disabled={busy === "test"}>
            {busy === "test" ? "Testing…" : "Test connection"}
          </Button>
          <Button variant="ghost" icon="refresh" onClick={() => void refresh()}>
            Refresh state
          </Button>
        </div>

        {!compact && (
          <details className="danger-zone">
            <summary>
              <Icon name="alert" size={16} />
              Unpair this laptop
            </summary>
            <p>
              Removing the local token stops the worker and wipes its credential. Server-side
              revocation needs operator mode; without it the server keeps a disabled-looking worker
              record until an operator removes it.
            </p>
            <Field label="Type UNPAIR to confirm">
              <input value={unpairText} onChange={(event) => setUnpairText(event.target.value)} placeholder="UNPAIR" />
            </Field>
            <label className="check">
              <input type="checkbox" checked={revokeRemote} onChange={(event) => setRevokeRemote(event.target.checked)} />
              <span>Also ask the server to revoke this worker (needs operator mode)</span>
            </label>
            <Button variant="danger" icon="power" onClick={unpair} disabled={busy === "unpair" || unpairText.trim() !== "UNPAIR"}>
              {busy === "unpair" ? "Unpairing…" : "Unpair device"}
            </Button>
          </details>
        )}
      </div>
    );
  }

  return (
    <div className={compact ? "pair-panel compact" : "pair-panel"}>
      <div className="pair-form-head">
        <span className="eyebrow">Server pairing</span>
        <h3>{compact ? "Pair a device" : "Connect to the control server"}</h3>
        <p>
          Create a code in the server dashboard under <span className="mono">Devices → Pair</span>. The
          code is single-use and expires after 10 minutes.
        </p>
      </div>

      <Field label="Server URL" hint="HTTPS is required unless the host is loopback or you explicitly accept plain HTTP.">
        <input value={server} onChange={(event) => setServer(event.target.value)} placeholder="https://mesh.example.com" spellCheck={false} />
      </Field>
      <Field label="Pairing code" hint="Case-insensitive. The server decides whether it is invalid, used or expired.">
        <input
          value={code}
          onChange={(event) => setCode(event.target.value.toUpperCase())}
          placeholder="ABCD-1234"
          className="code-input"
          spellCheck={false}
          autoComplete="off"
        />
      </Field>
      <Field label="Device name">
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder={snapshot.app.platform.split(" ")[0]} />
      </Field>

      {insecureNeeded && (
        <div className="insecure-block">
          <div className="inline-alert warn">
            <Icon name="shield" size={17} />
            <span>
              <strong>{parsed.host}</strong> is not loopback and this URL is plain HTTP. The worker token
              would travel in clear text over the network.
            </span>
          </div>
          <label className="check">
            <input type="checkbox" checked={allowInsecure} onChange={(event) => setAllowInsecure(event.target.checked)} />
            <span>I understand the risk and want to allow plain HTTP for this server</span>
          </label>
          {allowInsecure && (
            <Field label="Type ALLOW PLAIN HTTP">
              <input value={phrase} onChange={(event) => setPhrase(event.target.value)} placeholder="ALLOW PLAIN HTTP" />
            </Field>
          )}
        </div>
      )}

      {error && (
        <div className={`pair-error ${["pairing_used", "pairing_expired", "pairing_invalid"].includes(error.code) ? "code-state" : ""}`}>
          <Icon name="alert" size={17} />
          <div>
            <strong>{pairingErrorTitle(error.code)}</strong>
            <span>{error.message}</span>
            {error.code === "pairing_used" && <small>Ask the server dashboard for a brand-new code; the previous one cannot be reused.</small>}
            {error.code === "pairing_expired" && <small>Codes live for 10 minutes. Generate a fresh one and pair immediately.</small>}
            {error.code === "pairing_invalid" && <small>Check for typos and confirm the code belongs to this server.</small>}
          </div>
        </div>
      )}

      <div className="pair-submit">
        <Button icon="link" onClick={pair} disabled={busy === "pair" || !code.trim()}>
          {busy === "pair" ? "Pairing…" : "Pair device"}
        </Button>
        {compact && <span className="small muted">The worker starts automatically after pairing.</span>}
      </div>
    </div>
  );
}

function pairingErrorTitle(code: string): string {
  const map: Record<string, string> = {
    pairing_invalid: "Pairing code not recognised",
    pairing_used: "Pairing code already used",
    pairing_expired: "Pairing code expired",
    confirmation_required: "Explicit confirmation required",
    server_unreachable: "Server unreachable",
    protocol_mismatch: "Protocol mismatch",
    capacity: "Server at capacity"
  };
  return map[code] || "Pairing did not complete";
}
