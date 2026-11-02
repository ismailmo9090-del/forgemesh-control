/*
 * ForgeMesh Control Center — Electron desktop shell.
 *
 * The dashboard remains a normal loopback web app served by the Python control
 * server. This process owns the desktop concerns only: single instance, splash
 * with real startup stages, tray, close-to-tray, graceful shutdown, crash
 * recovery, optional autostart and window state. It never re-implements
 * ForgeMesh logic and it never invents agent status.
 */

"use strict";

const {
  app,
  BrowserWindow,
  Menu,
  Notification,
  Tray,
  ipcMain,
  nativeImage,
  screen,
  session,
  shell
} = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const IS_PACKAGED = app.isPackaged;
const PROJECT_ROOT = IS_PACKAGED
  ? path.join(process.resourcesPath, "backend")
  : path.resolve(__dirname, "..");
const DASHBOARD_DIST = IS_PACKAGED
  ? path.join(process.resourcesPath, "dashboard")
  : path.join(PROJECT_ROOT, "frontend", "dist");
const PYTHONPATH_ROOT = IS_PACKAGED
  ? path.join(process.resourcesPath, "backend-src")
  : path.join(PROJECT_ROOT, "src");

const HOST = "127.0.0.1";
const PORT = Number(process.env.FMC_PORT || 8787);
const BASE_URL = `http://${HOST}:${PORT}`;
const DATA_DIR =
  process.env.FMC_DATA_DIR ||
  path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share"), "forgemesh-control");
const TOKEN_PATH = path.join(DATA_DIR, "runtime", "control_token");
const BACKEND_PID_PATH = path.join(DATA_DIR, "runtime", "desktop_backend.pid");
const LOG_DIR = path.join(DATA_DIR, "logs");
const SERVER_LOG = path.join(LOG_DIR, "desktop-server.log");
const STATE_PATH = () => path.join(app.getPath("userData"), "window-state.json");
const AUTOSTART_PATH = path.join(os.homedir(), ".config", "autostart", "forgemesh-control-center.desktop");
// Explicit opt-in only: when set, the shell writes real PNGs of the splash and
// the dashboard at each startup stage. Nothing is captured unless a human asks.
const CAPTURE_PATH = process.env.FMC_DESKTOP_CAPTURE || "";

const STAGE_LABELS = [
  "Prepare environment",
  "Start control server",
  "Wait for HTTP health",
  "Open dashboard"
];

let mainWindow = null;
let splashWindow = null;
let tray = null;
let trayAvailable = false;
let backend = null;
let backendOwned = false;
let backendPid = null;
let backendExit = null;
let quitting = false;
let starting = false;
let stoppingBackend = false;
let restartAttempts = 0;
let lastExit = null;
let healthFailures = 0;
let trayTimer = null;
let watchdogTimer = null;
let splashCloseTimer = null;
let setupState = "unknown";
let serverMode = "stopped";
let toastShown = false;
let logTail = [];

const startup = {
  active: true,
  startedAt: Date.now(),
  stages: STAGE_LABELS.map((label) => ({ label, state: "pending", detail: "" })),
  error: "",
  elapsed: "0.0s"
};

if (!app.requestSingleInstanceLock()) {
  app.quit();
}

app.on("second-instance", () => {
  showMainWindow();
});

app.on("window-all-closed", () => {
  // The tray owns the lifetime. Closing the window only hides it.
});

app.on("before-quit", () => {
  quitting = true;
  stopTimers();
});

function ensureDirs() {
  fs.mkdirSync(LOG_DIR, { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.dirname(TOKEN_PATH), { recursive: true, mode: 0o700 });
}

// Electron on Linux dies on SIGTERM/SIGINT before a JS handler can run, so a
// killed shell always leaves its control server behind. Recording the owned pid
// makes that orphan findable, and the next shell adopts it instead of spawning
// a second server on the same port.
function recordBackendPid(pid) {
  if (!pid) return;
  try {
    fs.writeFileSync(BACKEND_PID_PATH, `${pid}\n`, { mode: 0o600 });
  } catch {
    /* the pid file is an aid, not a requirement */
  }
}

function clearBackendPid() {
  try {
    if (fs.existsSync(BACKEND_PID_PATH)) fs.unlinkSync(BACKEND_PID_PATH);
  } catch {
    /* best effort */
  }
}

function appendServerLog(line) {
  const stamp = new Date().toISOString();
  try {
    fs.appendFileSync(SERVER_LOG, `[${stamp}] ${line}\n`, { mode: 0o600 });
  } catch {
    /* logging must never crash the shell */
  }
  logTail.push(line);
  if (logTail.length > 120) logTail = logTail.slice(-120);
  if (splashWindow && !splashWindow.isDestroyed()) {
    splashWindow.webContents.send("startup:log", logTail.slice(-8).join("\n"));
  }
}

function setStage(index, state, detail = "") {
  if (index < 0 || index >= startup.stages.length) return;
  startup.stages[index] = { ...startup.stages[index], state, detail };
  pushStartup();
  if (CAPTURE_PATH && (state === "done" || state === "failed")) {
    void captureWindow(`stage${index + 1}-${state}`, splashWindow);
  }
}

let captureSeq = 0;

function captureWindow(label, target) {
  if (!CAPTURE_PATH || !target || target.isDestroyed()) return Promise.resolve("");
  captureSeq += 1;
  const extension = path.extname(CAPTURE_PATH) || ".png";
  const stem = CAPTURE_PATH.slice(0, CAPTURE_PATH.length - extension.length);
  const file = `${stem}-${String(captureSeq).padStart(2, "0")}-${label}${extension}`;
  return target.webContents
    .capturePage()
    .then((image) => {
      const png = image.toPNG();
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, png);
      appendServerLog(`captured ${label} -> ${file} (${png.length} bytes)`);
      return file;
    })
    .catch((error) => {
      appendServerLog(`capture failed for ${label}: ${error.message}`);
      return "";
    });
}

function pushStartup() {
  startup.elapsed = `${((Date.now() - startup.startedAt) / 1000).toFixed(1)}s`;
  if (!splashWindow || splashWindow.isDestroyed()) return;
  splashWindow.webContents.send("startup:state", {
    active: startup.active,
    stages: startup.stages,
    error: startup.error,
    elapsed: startup.elapsed,
    serverMode,
    log: logTail.slice(-8).join("\n")
  });
}

function resetStartup() {
  startup.active = true;
  startup.startedAt = Date.now();
  startup.stages = STAGE_LABELS.map((label) => ({ label, state: "pending", detail: "" }));
  startup.error = "";
  serverMode = "stopped";
  pushStartup();
}

function createSplash() {
  splashWindow = new BrowserWindow({
    width: 660,
    height: 480,
    resizable: false,
    frame: false,
    show: false,
    backgroundColor: "#f4f6f9",
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  splashWindow.loadFile(path.join(__dirname, "splash.html"));
  splashWindow.once("ready-to-show", () => {
    splashWindow.show();
    pushStartup();
    if (CAPTURE_PATH) void captureWindow("splash-open", splashWindow);
  });
  splashWindow.on("closed", () => {
    splashWindow = null;
  });
}

function loadWindowState() {
  try {
    const parsed = JSON.parse(fs.readFileSync(STATE_PATH(), "utf-8"));
    const bounds = parsed.bounds || {};
    if (
      Number.isFinite(bounds.width) &&
      Number.isFinite(bounds.height) &&
      bounds.width > 900 &&
      bounds.height > 600
    ) {
      const visible = screen.getAllDisplays().some((display) => {
        const area = display.workArea;
        return (
          bounds.x >= area.x - 40 &&
          bounds.y >= area.y - 40 &&
          bounds.x < area.x + area.width &&
          bounds.y < area.y + area.height
        );
      });
      if (visible) return bounds;
    }
  } catch {
    /* first run or corrupt state */
  }
  return { width: 1440, height: 920 };
}

function saveWindowState() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) return;
  const bounds = mainWindow.getNormalBounds();
  try {
    fs.writeFileSync(STATE_PATH(), JSON.stringify({ bounds }, null, 2));
  } catch {
    /* state is an optimisation */
  }
}

function createMainWindow({ load = false } = {}) {
  if (mainWindow && !mainWindow.isDestroyed()) return mainWindow;
  const bounds = loadWindowState();
  mainWindow = new BrowserWindow({
    ...bounds,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: "#f4f6f9",
    title: "ForgeMesh Control Center",
    icon: path.join(__dirname, "assets", "icon.png"),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false
    }
  });
  mainWindow.setMenuBarVisibility(false);
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://127.0.0.1:") || url.startsWith("http://localhost:")) {
      return { action: "allow" };
    }
    if (url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith(BASE_URL) && url !== "about:blank") {
      event.preventDefault();
      if (url.startsWith("https://")) shell.openExternal(url);
    }
  });
  mainWindow.on("close", (event) => {
    saveWindowState();
    if (quitting) return;
    // Hiding only makes sense while the tray owns the app lifetime. Without a
    // tray a hidden window is an unreachable app with a server still running,
    // so close really means quit.
    if (trayAvailable) {
      event.preventDefault();
      mainWindow.hide();
      appendServerLog("main window close request: hidden to tray");
      if (!toastShown) {
        toastShown = true;
        notify(
          "ForgeMesh is still running",
          "The control server keeps running in the tray. Choose Exit in the tray menu to stop it."
        );
      }
      return;
    }
    void quitApplication();
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
    if (CAPTURE_PATH) void captureWindow("dashboard", mainWindow);
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.close();
    }
  });
  if (load) {
    void loadDashboard(mainWindow).catch((error) => {
      appendServerLog(`dashboard reload failed: ${error.message}`);
    });
  }
  return mainWindow;
}

// The dashboard is only reachable with the control token, so a window that was
// recreated after being destroyed has to re-authenticate itself. startBackend
// used to be the only caller that loaded a URL, which left restored windows
// showing an empty document.
async function loadDashboard(target) {
  const token = await waitForToken();
  await target.loadURL(`${BASE_URL}/auth/session?token=${encodeURIComponent(token)}`);
  if (!target.isDestroyed() && !target.isVisible()) target.show();
  return token;
}

function needsDashboardLoad(target) {
  if (!target || target.isDestroyed()) return true;
  const url = target.webContents.getURL();
  // Nothing committed, or a document that is not the dashboard we serve.
  return !url || url === "about:blank" || !url.startsWith(BASE_URL);
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createMainWindow({ load: true });
  } else if (needsDashboardLoad(mainWindow)) {
    void loadDashboard(mainWindow).catch((error) => {
      appendServerLog(`dashboard reload failed: ${error.message}`);
    });
  }
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function notify(title, body) {
  try {
    if (Notification.isSupported()) {
      new Notification({ title, body, silent: true }).show();
    }
  } catch {
    /* notifications are optional on minimal desktops */
  }
}

function health() {
  return new Promise((resolve) => {
    const request = http.get(
      `${BASE_URL}/health`,
      { timeout: 900 },
      (response) => {
        let body = "";
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          try {
            const parsed = JSON.parse(body);
            resolve({ ok: response.statusCode === 200 && parsed.service === "forgemesh-control", body: parsed });
          } catch {
            resolve({ ok: false, body: null });
          }
        });
      }
    );
    request.on("timeout", () => request.destroy(new Error("timeout")));
    request.on("error", () => resolve({ ok: false, body: null }));
  });
}

function readToken() {
  try {
    const value = fs.readFileSync(TOKEN_PATH, "utf-8").trim();
    return value || "";
  } catch {
    return "";
  }
}

async function apiGet(pathname) {
  const token = readToken();
  if (!token) return null;
  return new Promise((resolve) => {
    const request = http.get(
      `${BASE_URL}${pathname}`,
      { timeout: 2500, headers: { "X-API-Token": token } },
      (response) => {
        let body = "";
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch {
            resolve(null);
          }
        });
      }
    );
    request.on("timeout", () => request.destroy(new Error("timeout")));
    request.on("error", () => resolve(null));
  });
}

function resolvePython() {
  const candidates = [
    process.env.FORGEMESH_PYTHON,
    process.env.FMC_PYTHON,
    path.join(PROJECT_ROOT, ".venv", "bin", "python"),
    path.join(os.homedir(), ".local", "share", "forgemesh-control", "venv", "bin", "python"),
    "/usr/bin/python3",
    "/usr/local/bin/python3"
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
    } catch {
      /* keep probing */
    }
  }
  return "";
}

function spawnBackend() {
  const python = resolvePython();
  if (!python) {
    throw new Error(
      "No Python interpreter was found. Install Python 3.11+ and the ForgeMesh backend dependencies."
    );
  }
  const env = {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    FMC_HOST: HOST,
    FMC_PORT: String(PORT),
    FMC_LOG_LEVEL: process.env.FMC_LOG_LEVEL || "INFO",
    PYTHONPATH: [PYTHONPATH_ROOT, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)
  };
  if (IS_PACKAGED) {
    env.FMC_FRONTEND_DIST = DASHBOARD_DIST;
  }
  ensureDirs();
  appendServerLog(`===== desktop launch: python ${python} project ${PROJECT_ROOT} =====`);
  const child = spawn(python, ["-m", "forgemesh_control.main", "serve"], {
    cwd: PROJECT_ROOT,
    env,
    stdio: ["ignore", "pipe", "pipe"],
    detached: false
  });
  backend = child;
  backendOwned = true;
  backendPid = child.pid || null;
  backendExit = null;
  serverMode = "running";
  recordBackendPid(backendPid);

  // appendServerLog is the only writer for this file: writing the raw chunk as
  // well duplicated every backend line, one stamped copy and one unstamped one.
  let pendingLine = "";
  const pipe = (chunk) => {
    pendingLine += chunk.toString();
    const lines = pendingLine.split(/\r?\n/);
    pendingLine = lines.pop();
    for (const line of lines) {
      if (line.trim()) appendServerLog(line.trim());
    }
  };
  child.stdout.on("data", (chunk) => pipe(chunk));
  child.stderr.on("data", (chunk) => pipe(chunk));

  child.on("error", (error) => {
    appendServerLog(`spawn error: ${error.message}`);
  });

  child.on("exit", (code, signal) => {
    if (pendingLine.trim()) appendServerLog(pendingLine.trim());
    lastExit = { code, signal, at: Date.now() };
    backendExit = { code, signal };
    if (backend === child) {
      backend = null;
      backendPid = null;
      clearBackendPid();
    }
    if (quitting || starting || stoppingBackend) return;
    handleCrash(code, signal);
  });
  return child;
}

async function handleCrash(code, signal) {
  serverMode = "stopped";
  appendServerLog(`control server exited (code=${code} signal=${signal})`);
  restartAttempts += 1;
  if (restartAttempts > 3) {
    notify(
      "ForgeMesh control server keeps stopping",
      `It exited ${restartAttempts} times (code ${code ?? "?"}). Open the log to see the real error.`
    );
    showStartupFailure(
      `The control server exited ${restartAttempts} times in a row (code ${code ?? "?"}).`,
      `Log: ${SERVER_LOG}`
    );
    return;
  }
  notify(
    "ForgeMesh control server stopped",
    `Restarting automatically (attempt ${restartAttempts} of 3).`
  );
  await delay(900 + restartAttempts * 700);
  if (quitting) return;
  try {
    startup.active = true;
    resetStartup();
    if (!splashWindow || splashWindow.isDestroyed()) createSplash();
    await startBackend({ recover: true });
  } catch (error) {
    showStartupFailure(`Restart failed: ${error.message}`, `Log: ${SERVER_LOG}`);
  }
}

function showStartupFailure(message, detail) {
  if (!splashWindow || splashWindow.isDestroyed()) {
    createSplash();
    setTimeout(() => {
      if (splashWindow && !splashWindow.isDestroyed()) {
        splashWindow.webContents.send("startup:state", {
          active: false,
          stages: startup.stages,
          error: message,
          elapsed: startup.elapsed,
          serverMode,
          log: `${logTail.slice(-6).join("\n")}\n${detail || ""}`.trim()
        });
      }
    }, 400);
  } else {
    splashWindow.show();
    splashWindow.webContents.send("startup:state", {
      active: false,
      stages: startup.stages,
      error: message,
      elapsed: startup.elapsed,
      serverMode,
      log: `${logTail.slice(-6).join("\n")}\n${detail || ""}`.trim()
    });
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForHealth(timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  let attempt = 0;
  while (Date.now() < deadline) {
    if (quitting) throw new Error("Application is quitting.");
    attempt += 1;
    const result = await health();
    if (result.ok) return true;
    const detail = `Waiting for ${BASE_URL} (${Math.round((Date.now() - deadline + timeoutMs) / 1000)}s)`;
    setStage(2, "active", detail);
    if (backendExit) {
      throw new Error(
        `Control server exited during startup (code ${backendExit.code ?? "?"}${
          backendExit.signal ? `, signal ${backendExit.signal}` : ""
        }). See ${SERVER_LOG}`
      );
    }
    await delay(attempt < 10 ? 350 : 900);
  }
  throw new Error(
    `The control server did not answer at ${BASE_URL}/health within ${Math.round(timeoutMs / 1000)} seconds.`
  );
}

async function waitForToken(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const token = readToken();
    if (token) return token;
    await delay(200);
  }
  throw new Error(`The control token file was not written: ${TOKEN_PATH}`);
}

async function startBackend({ recover = false } = {}) {
  // Reporting a silent no-op as a completed start would be a fake success, so a
  // re-entrant call says so out loud instead.
  if (starting) throw new Error("Startup already in progress.");
  starting = true;
  startup.active = true;
  if (!recover) resetStartup();
  startup.error = "";

  try {
    setStage(0, "active", "Resolving Python and dashboard build");
    const python = resolvePython();
    if (!python) {
      throw new Error(
        "No Python interpreter found. Expected .venv/bin/python in the project or python3 on PATH."
      );
    }
    if (!IS_PACKAGED && !fs.existsSync(path.join(DASHBOARD_DIST, "index.html"))) {
      throw new Error(
        "The dashboard build is missing. Run `npm --prefix frontend install && npm --prefix frontend run build`."
      );
    }
    const existing = await health();
    if (existing.ok) {
      serverMode = "attached";
      backendExit = null;
      // This shell did not start that process, so it must not claim to own it.
      clearBackendPid();
      setStage(
        0,
        "done",
        `Python ${python.replace(os.homedir(), "~")} · attaching to the already running server`
      );
      setStage(1, "done", `Existing control server on ${BASE_URL}`);
      setStage(2, "done", "HTTP health confirmed");
    } else {
      setStage(0, "done", `Python ${python.replace(os.homedir(), "~")}`);
      setStage(1, "active", "Launching the ForgeMesh control server");
      spawnBackend();
      setStage(1, "done", backendPid ? `Process ${backendPid} started` : "Process started");
      setStage(2, "active", `Polling ${BASE_URL}/health`);
      await waitForHealth();
      setStage(2, "done", "HTTP health confirmed");
    }

    setStage(3, "active", "Reading the local control token");
    const window = createMainWindow();
    await loadDashboard(window);
    setStage(3, "done", "Authenticated session prepared");
    mainWindow = window;
    startup.active = false;
    pushStartup();
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.close();
    }
    updateTray();
    startWatchdog();
    restartAttempts = 0;
  } catch (error) {
    startup.active = false;
    startup.error = error.message;
    const activeStage = startup.stages.findIndex((stage) => stage.state === "active");
    if (activeStage >= 0) setStage(activeStage, "failed", error.message);
    appendServerLog(`startup failed: ${error.message}`);
    showStartupFailure(error.message, `Log: ${SERVER_LOG}`);
    throw error;
  } finally {
    starting = false;
  }
}

// The dashboard holds a live /ws connection and reconnects when it drops.
// uvicorn waits for open connections to drain, so a bare SIGTERM ended up as
// "Waiting for connections to close" followed by a forceful SIGKILL. Destroying
// the web contents first tears those sockets down deterministically; navigating
// to about:blank was not enough because a hidden window is throttled and the
// navigation never committed. createMainWindow/loadDashboard rebuild the window
// on the next start, so nothing is lost by closing it here.
function releaseDashboardConnections() {
  try {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("shell:server-restarting");
      mainWindow.destroy();
      mainWindow = null;
    }
  } catch (error) {
    appendServerLog(`could not close the dashboard window: ${error.message}`);
  }
  return session.defaultSession
    .closeAllConnections()
    .catch((error) => appendServerLog(`could not close connections: ${error.message}`));
}

async function stopBackend(reason = "shutdown") {
  stopWatchdog();
  if (!backend) return;
  const child = backend;
  stoppingBackend = true;
  appendServerLog(`stopping control server (${reason}), pid ${child.pid}`);
  if (child.exitCode !== null || child.signalCode !== null) {
    appendServerLog("control server had already exited");
    backend = null;
    backendPid = null;
    serverMode = "stopped";
    stoppingBackend = false;
    return;
  }
  await releaseDashboardConnections();
  await new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      if (!settled) {
        appendServerLog("graceful stop timed out; sending SIGKILL");
        try {
          child.kill("SIGKILL");
        } catch {
          /* already gone */
        }
      }
    }, 8000);
    child.once("exit", finish);
    try {
      child.kill("SIGTERM");
    } catch {
      finish();
    }
  });
  backend = null;
  backendPid = null;
  serverMode = "stopped";
  stoppingBackend = false;
  clearBackendPid();
}

async function restartServer() {
  if (starting) return;
  notify("Restarting ForgeMesh", "Stopping the control server and starting it again.");
  await stopBackend("manual restart");
  try {
    resetStartup();
    if (!splashWindow || splashWindow.isDestroyed()) createSplash();
    await startBackend();
    showMainWindow();
  } catch {
    /* startBackend already surfaced the real error */
  }
}

function startWatchdog() {
  stopWatchdog();
  watchdogTimer = setInterval(async () => {
    if (quitting || starting) return;
    const result = await health();
    if (result.ok) {
      healthFailures = 0;
      return;
    }
    healthFailures += 1;
    appendServerLog(`health check failed (${healthFailures}/2)`);
    if (healthFailures >= 2) {
      healthFailures = 0;
      notify("ForgeMesh server unreachable", "Attempting a real restart and reload.");
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("shell:server-restarting");
      }
      try {
        await startBackend({ recover: true });
        if (mainWindow && !mainWindow.isDestroyed()) mainWindow.reload();
      } catch {
        /* the splash shows the failure */
      }
    }
  }, 6000);
}

function stopWatchdog() {
  if (watchdogTimer) clearInterval(watchdogTimer);
  watchdogTimer = null;
}

function trayImage() {
  const file = path.join(__dirname, "assets", "tray.png");
  const image = nativeImage.createFromPath(file);
  return image.isEmpty() ? nativeImage.createFromPath(path.join(__dirname, "assets", "icon.png")) : image;
}

function serverLabel() {
  if (serverMode === "attached") return "Server: attached to existing process";
  if (serverMode === "running") {
    return backendPid ? `Server: running (pid ${backendPid})` : "Server: running";
  }
  return "Server: stopped";
}

function setupLabel() {
  return `Setup: ${setupState.replace(/_/g, " ")}`;
}

async function refreshTrayState() {
  const status = await apiGet("/api/setup/status");
  if (status && typeof status.state === "string") {
    setupState = status.state;
  } else {
    setupState = "unreachable";
  }
  updateTray();
}

function updateTray() {
  if (!tray) return;
  const menu = Menu.buildFromTemplate([
    { label: "Open ForgeMesh Control Center", click: () => showMainWindow() },
    { label: serverLabel(), enabled: false },
    { label: setupLabel(), enabled: false },
    { type: "separator" },
    {
      label:
        serverMode === "attached"
          ? "Restart unavailable (external server)"
          : "Restart control server",
      enabled: serverMode !== "attached",
      click: () => {
        void restartServer();
      }
    },
    {
      label: "Open dashboard in browser",
      click: () => {
        const token = readToken();
        if (token) shell.openExternal(`${BASE_URL}/auth/session?token=${encodeURIComponent(token)}`);
        else shell.openExternal(BASE_URL);
      }
    },
    {
      label: "Open server log",
      click: () => shell.openPath(SERVER_LOG)
    },
    { type: "separator" },
    {
      label: "Start at login",
      type: "checkbox",
      checked: fs.existsSync(AUTOSTART_PATH),
      click: (item) => {
        setAutostart(item.checked);
        updateTray();
      }
    },
    { type: "separator" },
    {
      label: "Exit ForgeMesh",
      click: () => {
        void quitApplication();
      }
    }
  ]);
  tray.setContextMenu(menu);
  tray.setToolTip(`ForgeMesh Control Center — ${setupState}`);
}

function autostartState() {
  return {
    enabled: fs.existsSync(AUTOSTART_PATH),
    path: AUTOSTART_PATH,
    error: ""
  };
}

function setAutostart(enabled) {
  try {
    if (!enabled) {
      if (fs.existsSync(AUTOSTART_PATH)) fs.unlinkSync(AUTOSTART_PATH);
      notify("Autostart disabled", "ForgeMesh will not start automatically at login.");
      return autostartState();
    }
    fs.mkdirSync(path.dirname(AUTOSTART_PATH), { recursive: true });
    // Prefer the installed launcher wrapper: it carries the same --class the
    // applications entry declares, so the window groups with its own icon.
    const wrapper = path.join(
      process.env.XDG_BIN_HOME || path.join(os.homedir(), ".local", "bin"),
      "forgemesh-control-center"
    );
    const exec = app.isPackaged
      ? `"${process.env.APPIMAGE || process.execPath}"`
      : fs.existsSync(wrapper)
        ? `"${wrapper}"`
        : `"${process.execPath}" "${__dirname}"`;
    const content = [
      "[Desktop Entry]",
      "Type=Application",
      "Name=ForgeMesh Control Center",
      "Comment=AI multi-agent server control center",
      `Exec=${exec}`,
      `Icon=${path.join(__dirname, "assets", "icon.png")}`,
      "Terminal=false",
      "Categories=Development;",
      "X-GNOME-Autostart-enabled=true",
      ""
    ].join("\n");
    fs.writeFileSync(AUTOSTART_PATH, content, { mode: 0o644 });
    notify("Autostart enabled", `Launcher written to ${AUTOSTART_PATH}`);
    return autostartState();
  } catch (error) {
    notify("Could not change autostart", error.message);
    return { ...autostartState(), error: error.message };
  }
}

function createTray() {
  // Wayland sessions without a StatusNotifier host can fail here. A missing
  // tray must not stop the control server from starting, and close-to-tray is
  // disabled when this fails, so record the real outcome instead of assuming it.
  try {
    tray = new Tray(trayImage());
    tray.setToolTip("ForgeMesh Control Center");
    tray.on("click", () => showMainWindow());
    tray.on("double-click", () => showMainWindow());
    trayAvailable = true;
    appendServerLog("tray created");
  } catch (error) {
    tray = null;
    trayAvailable = false;
    appendServerLog(`tray unavailable: ${error.message}`);
  }
  updateTray();
  trayTimer = setInterval(() => {
    void refreshTrayState();
  }, 12000);
  void refreshTrayState();
}

async function quitApplication() {
  if (quitting) return;
  quitting = true;
  stopTimers();
  saveWindowState();
  await stopBackend("application exit");
  app.quit();
}

function stopTimers() {
  stopWatchdog();
  if (trayTimer) clearInterval(trayTimer);
  trayTimer = null;
  if (splashCloseTimer) clearTimeout(splashCloseTimer);
  splashCloseTimer = null;
}

ipcMain.on("startup:retry", () => {
  restartAttempts = 0;
  void (async () => {
    resetStartup();
    try {
      await startBackend();
    } catch {
      /* failure is rendered in the splash */
    }
  })();
});

ipcMain.on("app:quit", () => {
  void quitApplication();
});

ipcMain.handle("shell:info", () => ({
  version: app.getVersion(),
  electron: process.versions.electron,
  platform: process.platform,
  serverMode,
  backendPid,
  backendPidFile: BACKEND_PID_PATH,
  baseUrl: BASE_URL,
  tokenPath: TOKEN_PATH,
  logPath: SERVER_LOG,
  packaged: IS_PACKAGED,
  autostart: autostartState(),
  trayAvailable,
  windowExists: Boolean(mainWindow && !mainWindow.isDestroyed()),
  windowVisible: Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible()),
  windowUrl: mainWindow && !mainWindow.isDestroyed() ? mainWindow.webContents.getURL() : ""
}));

ipcMain.handle("shell:autostart", () => autostartState());

ipcMain.handle("shell:set-autostart", (_event, enabled) => {
  const result = setAutostart(Boolean(enabled));
  updateTray();
  return result;
});

ipcMain.handle("shell:open-log", () => shell.openPath(SERVER_LOG));

ipcMain.handle("shell:restart", async () => {
  if (starting) return { ok: false, error: "Startup already in progress." };
  if (serverMode === "attached") {
    return { ok: false, error: "This server is external; the shell does not own its process." };
  }
  await restartServer();
  return { ok: true, error: "" };
});

ipcMain.handle("shell:hide", () => {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.hide();
  return true;
});

// Routes through BrowserWindow.close(), i.e. exactly what the title bar button
// does, so the close-to-tray interception is the thing being exercised.
ipcMain.handle("shell:close-window", () => {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close();
  return true;
});

app.whenReady().then(async () => {
  app.setName("ForgeMesh Control Center");
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });
  ensureDirs();
  createSplash();
  createTray();
  try {
    await startBackend();
  } catch {
    /* the splash keeps the retry and exit controls */
  }
  app.on("activate", () => showMainWindow());
});

process.on("uncaughtException", (error) => {
  appendServerLog(`uncaught: ${error.stack || error.message}`);
});

process.on("unhandledRejection", (error) => {
  appendServerLog(`unhandled rejection: ${error && error.stack ? error.stack : String(error)}`);
});
