#!/usr/bin/env node
/*
 * Chrome DevTools Protocol probe for the ForgeMesh desktop shell.
 *
 * It talks to an Electron instance started with --remote-debugging-port and
 * lets us read real renderer state (Runtime.evaluate) and grab real pixels
 * (Page.captureScreenshot) instead of trusting the shell's own log lines.
 *
 * There is deliberately no third-party dependency: the project does not ship
 * `ws`, and Node 22 already exposes a global WebSocket, which is all CDP needs.
 *
 * Usage:
 *   node desktop/tools/cdp_probe.js targets
 *   node desktop/tools/cdp_probe.js eval   "<js expression>" [--match <url part>] [--timeout ms]
 *   node desktop/tools/cdp_probe.js wait   "<js expression>" [--match <url part>] [--timeout ms]
 *   node desktop/tools/cdp_probe.js shot   <out.png>        [--match <url part>]
 *
 * Environment:
 *   FMC_CDP_HOST (default 127.0.0.1)  FMC_CDP_PORT (default 9333)
 */

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");

const HOST = process.env.FMC_CDP_HOST || "127.0.0.1";
const PORT = Number(process.env.FMC_CDP_PORT || 9333);

function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg.startsWith("--")) flags[arg.slice(2)] = argv[i + 1] ?? true;
    else positional.push(arg);
  }
  return { positional, flags };
}

function httpJson(pathname) {
  return new Promise((resolve, reject) => {
    const request = http.get({ host: HOST, port: PORT, path: pathname, timeout: 4000 }, (response) => {
      let body = "";
      response.on("data", (chunk) => {
        body += chunk;
      });
      response.on("end", () => {
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(new Error(`bad JSON from /json: ${body.slice(0, 200)}`));
        }
      });
    });
    request.on("timeout", () => request.destroy(new Error("CDP HTTP timeout")));
    request.on("error", (error) => reject(new Error(`CDP unreachable on ${HOST}:${PORT}: ${error.message}`)));
  });
}

class Session {
  constructor(webSocketUrl) {
    this.webSocketUrl = webSocketUrl;
    this.nextId = 1;
    this.pending = new Map();
    this.socket = null;
  }

  async connect() {
    this.socket = new WebSocket(this.webSocketUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("CDP websocket connect timeout")), 8000);
      this.socket.addEventListener("open", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
      this.socket.addEventListener("error", (event) => {
        clearTimeout(timer);
        reject(new Error(event.message || "CDP websocket error"));
      }, { once: true });
    });
    this.socket.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (!message.id) return;
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.error) waiter.reject(new Error(`${message.error.code}: ${message.error.message}`));
      else waiter.resolve(message.result);
    });
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    const payload = JSON.stringify({ id, method, params });
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(payload);
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`${method} timed out`));
        }
      }, 30000).unref?.();
    });
  }

  close() {
    try {
      this.socket?.close();
    } catch {
      /* already gone */
    }
  }
}

async function openTarget(match) {
  const targets = await httpJson("/json/list");
  const pages = targets.filter((target) => target.type === "page" && target.webSocketDebuggerUrl);
  if (match) {
    const hit = pages.find((target) => (target.url || "").includes(match) || (target.title || "").includes(match));
    if (!hit) throw new Error(`no page target matching ${JSON.stringify(match)}; have: ${pages.map((t) => t.url).join(", ") || "none"}`);
    return hit;
  }
  if (!pages.length) throw new Error("no page targets on this Electron instance");
  return pages[0];
}

async function withSession(match, run) {
  const target = await openTarget(match);
  const session = new Session(target.webSocketDebuggerUrl);
  await session.connect();
  try {
    return await run(session, target);
  } finally {
    session.close();
  }
}

async function evaluate(session, expression) {
  const result = await session.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (result.exceptionDetails) {
    throw new Error(`page threw: ${result.exceptionDetails.exception?.description || result.exceptionDetails.text}`);
  }
  return result.result?.value;
}

async function main() {
  const argv = process.argv.slice(2);
  const command = argv[0];
  const { positional, flags } = parseArgs(argv.slice(1));
  const match = typeof flags.match === "string" ? flags.match : undefined;
  const timeout = Number(flags.timeout || 60000);

  if (command === "targets") {
    const targets = await httpJson("/json/list");
    for (const target of targets) {
      console.log(`${target.type}\t${target.url}\t${target.title}\t${target.webSocketDebuggerUrl ? "cdp" : "-"}`);
    }
    return;
  }

  if (command === "eval") {
    const expression = positional[0];
    if (!expression) throw new Error("eval needs an expression");
    const value = await withSession(match, (session) => evaluate(session, expression));
    console.log(JSON.stringify(value ?? null, null, 2));
    return;
  }

  if (command === "wait") {
    const expression = positional[0];
    if (!expression) throw new Error("wait needs an expression");
    const deadline = Date.now() + timeout;
    let lastError = null;
    while (Date.now() < deadline) {
      try {
        const value = await withSession(match, (session) => evaluate(session, `(async () => (${expression}))()`)
          .catch(async (error) => {
            lastError = error;
            return undefined;
          }));
        if (value) {
          console.log(JSON.stringify(value, null, 2));
          return;
        }
      } catch (error) {
        lastError = error;
      }
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    throw new Error(`timed out after ${timeout}ms waiting for: ${expression}${lastError ? ` (last: ${lastError.message})` : ""}`);
  }

  if (command === "shot") {
    const output = positional[0];
    if (!output) throw new Error("shot needs an output path");
    const data = await withSession(match, async (session, target) => {
      await session.send("Page.enable");
      // A hidden window has no surface to read; ask the shell to show it first
      // rather than writing an empty PNG and calling it a screenshot.
      const visible = await evaluate(session, "document.visibilityState").catch(() => "unknown");
      if (visible === "hidden") console.log(`warning: target ${target.url} reports document.hidden`);
      // A window with no committed document has no usable execution context, so
      // never let the settle wait abort the capture.
      await evaluate(session, "new Promise((r) => requestAnimationFrame(() => setTimeout(r, 120)))").catch(() => {});
      const result = await session.send("Page.captureScreenshot", { format: "png" });
      return Buffer.from(result.data, "base64");
    });
    fs.mkdirSync(path.dirname(path.resolve(output)), { recursive: true });
    fs.writeFileSync(output, data);
    console.log(`wrote ${path.resolve(output)} (${data.length} bytes)`);
    return;
  }

  throw new Error(`unknown command ${JSON.stringify(command)} (use targets | eval | wait | shot)`);
}

main().catch((error) => {
  console.error(`ERROR: ${error.message}`);
  process.exit(1);
});
