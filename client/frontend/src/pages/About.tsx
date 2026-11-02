import { useState } from "react";
import { shell } from "../api";
import { useApp } from "../context";
import { Button, CopyButton, Icon, Pill, SectionHeader, formatDate, uptime } from "../components/ui";

export default function About() {
  const { snapshot } = useApp();
  const { app, connection, security } = snapshot;
  const [info, setInfo] = useState<Record<string, unknown> | null>(null);

  return (
    <div className="page about">
      <div className="about-hero card">
        <div className="about-mark">
          <span /><span /><span />
        </div>
        <div>
          <span className="eyebrow">ForgeMesh</span>
          <h1>Client Manager</h1>
          <p>
            A Linux worker and output-laptop shell for the ForgeMesh control server. It supervises the
            existing Python worker, hosts the two supported agent CLIs locally, and never reimplements
            mesh protocol v1.
          </p>
          <div className="row">
            <Pill tone="blue">v{app.version}</Pill>
            <Pill tone="violet">protocol {app.protocol_version}</Pill>
            <Pill tone={security.insecure_http ? "red" : "green"}>{security.insecure_http ? "plain HTTP acknowledged" : "secure transport"}</Pill>
          </div>
        </div>
        <Button
          size="sm"
          variant="secondary"
          icon="info"
          onClick={async () => setInfo(await shell.info())}
        >
          Shell info
        </Button>
      </div>

      {info && (
        <section className="card">
          <div className="card-head"><div><h2>Electron shell</h2><p>Versions reported by the desktop process.</p></div><CopyButton value={JSON.stringify(info, null, 2)} /></div>
          <pre className="code-block">{JSON.stringify(info, null, 2)}</pre>
        </section>
      )}

      <div className="grid-2">
        <section className="card">
          <div className="card-head"><div><h2>Runtime</h2><p>What this process actually reports.</p></div></div>
          <dl className="kv">
            <div><dt>App name</dt><dd>{app.name}</dd></div>
            <div><dt>App version</dt><dd className="mono">{app.version}</dd></div>
            <div><dt>Mesh protocol</dt><dd className="mono">{app.protocol_version}</dd></div>
            <div><dt>Platform</dt><dd>{app.platform}</dd></div>
            <div><dt>Architecture</dt><dd className="mono">{app.arch}</dd></div>
            <div><dt>Python</dt><dd className="mono">{app.python}</dd></div>
            <div><dt>PTY available</dt><dd>{app.pty ? "yes" : "no"}</dd></div>
            <div><dt>Bridge started</dt><dd>{formatDate(app.started_at)}</dd></div>
            <div><dt>Uptime</dt><dd>{uptime(app.started_at)}</dd></div>
          </dl>
        </section>

        <section className="card">
          <div className="card-head"><div><h2>Identity</h2><p>Public fingerprints only; tokens are never shown.</p></div></div>
          <dl className="kv">
            <div><dt>Worker name</dt><dd>{connection.worker_name || "—"}</dd></div>
            <div><dt>Worker ID</dt><dd className="mono">{connection.worker_id || "—"}</dd></div>
            <div><dt>Token fingerprint</dt><dd className="mono">{connection.token_fingerprint || "—"}</dd></div>
            <div><dt>Device key fingerprint</dt><dd className="mono">{app.device_key_fingerprint}</dd></div>
            <div><dt>Token backend</dt><dd>{security.secret_backend || "none"}</dd></div>
            <div><dt>Operator token</dt><dd>{security.has_operator_token ? "present (separate store)" : "not configured"}</dd></div>
          </dl>
        </section>
      </div>

      <section className="card">
        <div className="card-head"><div><h2>Product boundaries</h2><p>Deliberate choices, not missing screens.</p></div></div>
        <div className="boundary-grid">
          <div>
            <h4><Icon name="check" size={15} /> Does</h4>
            <ul>
              <li>Pairs with a single-use code and stores the worker token privately.</li>
              <li>Supervises the real <span className="mono">worker_client</span> loop as a child process.</li>
              <li>Installs the two documented agent CLIs using official sources.</li>
              <li>Hosts a real PTY for sign-in and leaves challenges to the human.</li>
              <li>Shows task progress, logs and server-derived delivery evidence.</li>
            </ul>
          </div>
          <div>
            <h4><Icon name="close" size={15} /> Does not</h4>
            <ul>
              <li>Reimplement mesh frames, reconnect semantics or task execution.</li>
              <li>Call <span className="mono">/api/agents/*</span> to manage the server's registry.</li>
              <li>Type credentials, solve CAPTCHA/OTP, or choose a model for a task.</li>
              <li>Invent projects, approvals or artifact downloads the server does not expose.</li>
              <li>Use the worker token for dashboard routes; operator mode is explicit.</li>
            </ul>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card-head"><div><h2>Local paths and logs</h2><p>Everything the daemon writes.</p></div></div>
        <dl className="kv wide">
          {Object.entries(app.paths).map(([name, value]) => (
            <div key={name}><dt>{name}</dt><dd className="mono ellipsis">{value}</dd></div>
          ))}
        </dl>
        <div className="row top-gap">
          <Button size="sm" variant="secondary" icon="external" onClick={() => void shell.openLog()}>Open bridge log</Button>
          <Button size="sm" variant="ghost" icon="folder" onClick={async () => setInfo(await shell.info())}>Refresh shell info</Button>
        </div>
      </section>
    </div>
  );
}
