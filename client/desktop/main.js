/*
 * ForgeMesh Client Manager — Electron desktop shell.
 *
 * Owns only desktop concerns: a splash with real startup stages, the local
 * Python bridge process and its random bearer token, IPC proxying, tray,
 * autostart, window state and optional capture mode for evidence.
 *
 * It does not re-implement mesh protocol v1, agent detection or task
 * execution. Those live in the Python modules shipped with the app.
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
  : path.resolve(__dirname, "..", "..");
const FRONTEND_DIST = IS_PACKAGED
  ? path.join(process.resourcesPath, "client-frontend")
  : path.resolve(__dirname, "..", "frontend", "dist");
const PYTHONPATH_ROOT = IS_PACKAGED
  ? path.join(process.resourcesPath, "backend-src")
  : path.join(PROJECT_ROOT, "src");
const CAPTURE_DIR = process.env.FMCM_CAPTURE_DIR || "";
const DEV_URL = process.env.FMCM_DEV_URL || "";
const AUTOSTART_PATH = path.join(os.homedir(), ".config", "autostart", "forgemesh-client-manager.desktop");
const LOG_DIR = path.join(os.homedir(), ".local", "share", "forgemesh-client-manager", "logs");
const LOG_PATH = path.join(LOG_DIR, "desktop.log");

const STAGE_LABELS = [
  "Start local bridge",
  "Read handshake",
  "Wait for health",
  "Load interface"
];

let mainWindow = null;
let splashWindow = null;
let tray = null;
let trayAvailable = false;
let daemon = null;
let daemonInfo = null;
let daemonState = "starting";
let daemonLogTail = [];
let daemonStream = null;
let healthTimer = null;
let socket = null;
let socketRetry = null;
let quitting = false;
let starting = false;
let startingPromise = null;
let restartAttempts = 0;
let captureRunning = false;

const startup = {
  stages: STAGE_LABELS.map((label) => ({ label, state: "pending", detail: "" })),
  error: "",
  progress: 0
};

if (!app.requestSingleInstanceLock()) {
  app.quit();
}

app.on("second-instance", () => showMainWindow());

app.on("window-all-closed", () => {
  // The tray owns the lifetime.
});

app.on("before-quit", () => {
  quitting = true;
  stopHealthLoop();
  closeSocket();
});

function ensureDirs() {
  for (const directory of [LOG_DIR, path.dirname(AUTOSTART_PATH)]) {
    try {
      fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    } catch {
      /* reported when the log is opened */
    }
  }
}

function log(message) {
  const line = `[${new Date().toISOString()}] ${message}\n`;
  try {
    fs.appendFileSync(LOG_PATH, line, { mode: 0o600 });
  } catch {
    /* the app still runs if the log is unwritable */
  }
  daemonLogTail.push(line.trim());
  if (daemonLogTail.length > 400) daemonLogTail = daemonLogTail.slice(-400);
}

function setStage(index, state, detail = "") {
  startup.stages[index] = { ...startup.stages[index], state, detail };
  startup.progress = Math.round(
    (startup.stages.filter((stage) => stage.state === "done").length / startup.stages.length) * 100
  );
  if (splashWindow && !splashWindow.isDestroyed()) {
    splashWindow.webContents.send("splash:state", {
      stages: startup.stages,
      progress: startup.progress,
      error: startup.error
    });
  }
}

function pythonCommand() {
  const candidates = [
    process.env.FMCM_PYTHON,
    IS_PACKAGED ? path.join(process.resourcesPath, "backend", "venv", "bin", "python") : "",
    "/opt/forgemesh-client/venv/bin/python",
    "/opt/forgemesh/venv/bin/python",
    path.join(PROJECT_ROOT, ".venv", "bin", "python"),
    "/usr/bin/python3"
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {
      /* try the next one */
    }
  }
  return IS_PACKAGED ? "/usr/bin/python3" : "python3";
}

function startDaemon() {
  if (startingPromise) return startingPromise;
  starting = true;
  startup.error = "";
  setStage(0, "active", pythonCommand());
  log(`starting local bridge with ${pythonCommand()}`);

  startingPromise = new Promise((resolve, reject) => {
    const env = {
      ...process.env,
      PYTHONPATH: PYTHONPATH_ROOT + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : ""),
      PYTHONUNBUFFERED: "1",
      FMCM_PYTHON: pythonCommand()
    };
    let settled = false;
    const child = spawn(
      pythonCommand(),
      ["-m", "forgemesh_control.client_manager", "--handshake", "--port", "0"],
      { cwd: PROJECT_ROOT, env, stdio: ["ignore", "pipe", "pipe"] }
    );
    daemon = child;

    let buffer = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        if (!daemonInfo) {
          try {
            daemonInfo = JSON.parse(line);
            log(`handshake on 127.0.0.1:${daemonInfo.port} pid ${daemonInfo.pid}`);
            if (process.env.FMCM_HANDSHAKE_OUT) {
              try {
                fs.writeFileSync(
                  path.resolve(process.env.FMCM_HANDSHAKE_OUT),
                  JSON.stringify({ ...daemonInfo, at: new Date().toISOString() }, null, 2),
                  { mode: 0o600 }
                );
              } catch (error) {
                log(`could not write handshake file: ${error.message}`);
              }
            }
            setStage(0, "done", `pid ${daemonInfo.pid}`);
            setStage(1, "done", `127.0.0.1:${daemonInfo.port}`);
            setStage(2, "active", "GET /health");
            settled = true;
            resolve(daemonInfo);
          } catch (error) {
            log(`unparseable handshake: ${line}`);
            startup.error = "The local bridge printed an unreadable handshake.";
            reject(new Error(startup.error));
          }
        } else {
          log(`daemon: ${line}`);
        }
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      for (const line of String(chunk).split("\n")) {
        if (line.trim()) log(`daemon stderr: ${line.trim()}`);
      }
    });
    child.on("error", (error) => {
      startup.error = `Could not start Python: ${error.message}`;
      setStage(0, "failed", error.message);
      if (!settled) reject(error);
    });
    child.on("exit", (code, signal) => {
      log(`local bridge exited code=${code} signal=${signal}`);
      daemon = null;
      daemonInfo = null;
      closeSocket();
      if (!settled) {
        startup.error = `The local bridge exited before its handshake (code ${code}).`;
        reject(new Error(startup.error));
        return;
      }
      setDaemonState("down");
      if (!quitting) {
        if (restartAttempts < 3) {
          restartAttempts += 1;
          setTimeout(() => {
            void startDaemon().catch(() => undefined);
          }, 1500);
        }
      }
    });
    setTimeout(() => {
      if (!settled) {
        startup.error = "Timed out waiting for the local bridge handshake.";
        setStage(0, "failed", startup.error);
        try {
          child.kill("SIGTERM");
        } catch {
          /* already gone */
        }
        reject(new Error(startup.error));
      }
    }, 30000);
  }).finally(() => {
    starting = false;
    startingPromise = null;
  });

  return startingPromise;
}

function stopDaemon() {
  if (!daemon) return Promise.resolve();
  const child = daemon;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        /* gone */
      }
      resolve();
    }, 4000);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    try {
      child.kill("SIGTERM");
    } catch {
      clearTimeout(timer);
      resolve();
    }
  });
}

function setDaemonState(state) {
  daemonState = state;
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("bridge:state", state);
  }
  updateTray();
}

async function checkHealth() {
  if (!daemonInfo) return false;
  return new Promise((resolve) => {
    const request = http.get(
      { host: "127.0.0.1", port: daemonInfo.port, path: "/health", timeout: 2500 },
      (response) => {
        response.resume();
        resolve(response.statusCode === 200);
      }
    );
    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });
    request.on("error", () => resolve(false));
  });
}

function startHealthLoop() {
  stopHealthLoop();
  healthTimer = setInterval(async () => {
    const ok = await checkHealth();
    if (!ok && daemonState === "up") {
      setDaemonState("down");
      log("health check failed");
    } else if (ok && daemonState !== "up") {
      setDaemonState("up");
      connectSocket();
    }
  }, 4000);
}

function stopHealthLoop() {
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = null;
}

async function connectSocket() {
  closeSocket();
  if (!daemonInfo) return;
  try {
    socket = new WebSocket(`ws://127.0.0.1:${daemonInfo.port}/ws?token=${encodeURIComponent(daemonInfo.token)}`);
  } catch (error) {
    log(`websocket construct failed: ${error.message}`);
    scheduleSocketRetry();
    return;
  }
  socket.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(String(event.data));
      if (payload.type === "event" && payload.event) {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send("bridge:event", payload.event);
        }
        maybeNotify(payload.event);
      }
    } catch {
      /* a malformed event is ignored, not rendered */
    }
  });
  socket.addEventListener("close", () => {
    socket = null;
    if (!quitting && daemonState === "up") scheduleSocketRetry();
  });
  socket.addEventListener("error", () => {
    /* close handles retry */
  });
}

function scheduleSocketRetry() {
  if (socketRetry || quitting) return;
  socketRetry = setTimeout(() => {
    socketRetry = null;
    if (daemonState === "up") void connectSocket();
  }, 2500);
}

function closeSocket() {
  if (socket) {
    try {
      socket.close();
    } catch {
      /* already closed */
    }
    socket = null;
  }
  if (socketRetry) clearTimeout(socketRetry);
  socketRetry = null;
}

function maybeNotify(event) {
  if (!Notification.isSupported()) return;
  const kind = String(event.kind || "");
  if (!["worker.disconnected", "connection.failed", "pairing.completed", "agent.auth.finished", "worker.revoked"].includes(kind)) {
    return;
  }
  try {
    new Notification({
      title: "ForgeMesh Client Manager",
      body: String(event.message || kind),
      silent: false
    }).show();
  } catch {
    /* notifications are best effort */
  }
}

function daemonHeaders() {
  return { Authorization: `Bearer ${daemonInfo ? daemonInfo.token : ""}` };
}

async function bridgeRequest(method, requestPath, body) {
  if (!daemonInfo) {
    return { ok: false, status: 0, data: { detail: { code: "bridge_down", message: "The local bridge is not running." } } };
  }
  try {
    const response = await fetch(`http://127.0.0.1:${daemonInfo.port}${requestPath}`, {
      method,
      headers: { "Content-Type": "application/json", ...daemonHeaders() },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(960000)
    });
    let payload = null;
    const text = await response.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { detail: text.slice(0, 2000) };
      }
    }
    return { ok: response.ok, status: response.status, data: payload };
  } catch (error) {
    return {
      ok: false,
      status: 0,
      data: { detail: { code: "bridge_unreachable", message: `Local bridge request failed: ${error.message}` } }
    };
  }
}

// ---------------------------------------------------------------------- windows
function createSplash() {
  splashWindow = new BrowserWindow({
    width: 520,
    height: 430,
    resizable: false,
    frame: false,
    show: true,
    center: true,
    backgroundColor: "#ffffff",
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "splash.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  splashWindow.loadFile(path.join(__dirname, "splash.html"));
  splashWindow.on("closed", () => {
    splashWindow = null;
  });
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 880,
    minWidth: 980,
    minHeight: 640,
    show: false,
    backgroundColor: "#f4f6f9",
    title: "ForgeMesh Client Manager",
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      spellcheck: false
    }
  });

  if (DEV_URL) {
    mainWindow.loadURL(DEV_URL);
  } else {
    mainWindow.loadFile(path.join(FRONTEND_DIST, "index.html"));
  }

  const revealWindow = () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (!mainWindow.isVisible()) mainWindow.show();
    if (splashWindow && !splashWindow.isDestroyed()) splashWindow.close();
    setStage(3, "done", "window ready");
  };
  mainWindow.once("ready-to-show", revealWindow);
  // Some Wayland/compositor combinations never emit ready-to-show when GPU
  // compositing is unavailable. A completed load is still a usable window.
  mainWindow.webContents.once("did-finish-load", revealWindow);

  mainWindow.on("close", (event) => {
    if (!quitting && trayAvailable) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https:\/\//.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });

  if (CAPTURE_DIR) {
    mainWindow.webContents.once("did-finish-load", () => {
      setTimeout(() => void runCapture(), 5000);
    });
  }
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createMainWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

async function waitForMainWindow(timeout = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.webContents.isLoading()) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function runCapture() {
  if (captureRunning) return;
  captureRunning = true;
  const directory = path.resolve(CAPTURE_DIR);
  fs.mkdirSync(directory, { recursive: true, mode: 0o755 });
  await waitForMainWindow();
  const pages = [
    ["01-dashboard", 1],
    ["02-agents", 2],
    ["03-tasks", 3],
    ["04-review", 4],
    ["05-approvals", 5],
    ["06-activity", 6],
    ["07-settings", 7],
    ["08-about", 8]
  ];
  for (const [name, index] of pages) {
    try {
      await mainWindow.webContents.executeJavaScript(
        `document.querySelectorAll('.sidebar nav button')[${index - 1}]?.click(); document.querySelector('.content')?.scrollTo(0,0); window.scrollTo(0,0); true;`
      );
      await new Promise((resolve) => setTimeout(resolve, 1200));
      const image = await mainWindow.webContents.capturePage();
      fs.writeFileSync(path.join(directory, `${name}.png`), image.toPNG());
      log(`captured ${name}.png`);
    } catch (error) {
      log(`capture ${name} failed: ${error.message}`);
    }
  }
  // Pairing/identity modal is part of the evidence set.
  try {
    await mainWindow.webContents.executeJavaScript(
      `document.querySelector('.pair-card')?.click(); true;`
    );
    await new Promise((resolve) => setTimeout(resolve, 900));
    const image = await mainWindow.webContents.capturePage();
    fs.writeFileSync(path.join(directory, "09-pairing-modal.png"), image.toPNG());
    await mainWindow.webContents.executeJavaScript(
      `document.querySelector('.modal .icon-btn')?.click(); true;`
    );
  } catch (error) {
    log(`capture modal failed: ${error.message}`);
  }
  log("capture complete");
  captureRunning = false;
}

// ------------------------------------------------------------------------- tray
function createTray() {
  try {
    const icon = nativeImage.createFromPath(path.join(__dirname, "assets", "tray.png"));
    tray = new Tray(icon);
    tray.setToolTip("ForgeMesh Client Manager");
    updateTray();
    tray.on("click", () => showMainWindow());
    tray.on("double-click", () => showMainWindow());
    trayAvailable = true;
  } catch (error) {
    trayAvailable = false;
    log(`tray unavailable: ${error.message}`);
  }
}

function updateTray() {
  if (!tray) return;
  const label = daemonState === "up" ? "Connected" : daemonState === "down" ? "Bridge offline" : "Starting";
  const menu = Menu.buildFromTemplate([
    { label: `Local bridge: ${label}`, enabled: false },
    { type: "separator" },
    { label: "Open Client Manager", click: () => showMainWindow() },
    {
      label: "Start worker",
      click: async () => {
        const result = await bridgeRequest("POST", "/api/worker/start", { takeover: false });
        log(`tray start worker: ${result.status}`);
        if (!result.ok && Notification.isSupported()) {
          new Notification({ title: "Worker start failed", body: String(result.data?.detail?.message || "See the app") }).show();
        }
      }
    },
    {
      label: "Stop worker",
      click: async () => {
        const result = await bridgeRequest("POST", "/api/worker/stop", { confirm: true, stop_unit: false });
        log(`tray stop worker: ${result.status}`);
      }
    },
    { type: "separator" },
    { label: "Open log", click: () => void shell.openPath(LOG_PATH) },
    { label: "Quit", click: () => void quitApplication() }
  ]);
  tray.setContextMenu(menu);
}

// --------------------------------------------------------------------- autostart
function autostartState() {
  try {
    return { enabled: fs.existsSync(AUTOSTART_PATH), path: AUTOSTART_PATH };
  } catch {
    return { enabled: false, path: AUTOSTART_PATH };
  }
}

function setAutostart(enabled) {
  if (!enabled) {
    try {
      fs.rmSync(AUTOSTART_PATH, { force: true });
    } catch (error) {
      return { enabled: false, path: AUTOSTART_PATH, error: error.message };
    }
    return autostartState();
  }
  const exec = process.env.APPIMAGE || process.execPath;
  const args = IS_PACKAGED
    ? "--hidden"
    : `"${app.getAppPath()}" --ozone-platform-hint=auto`;
  const entry = `[Desktop Entry]
Type=Application
Name=ForgeMesh Client Manager
Comment=Supervise the ForgeMesh worker and review output
Exec=${exec} ${args}
Icon=${path.join(__dirname, "assets", "icon.png")}
Terminal=false
Categories=Development;
X-GNOME-Autostart-enabled=true
`;
  try {
    fs.mkdirSync(path.dirname(AUTOSTART_PATH), { recursive: true });
    fs.writeFileSync(AUTOSTART_PATH, entry, { mode: 0o644 });
    return autostartState();
  } catch (error) {
    return { enabled: false, path: AUTOSTART_PATH, error: error.message };
  }
}

// ------------------------------------------------------------------ lifecycle
async function boot() {
  createSplash();
  try {
    await startDaemon();
    setStage(2, "active", "GET /health");
    let healthy = false;
    for (let attempt = 0; attempt < 60; attempt += 1) {
      healthy = await checkHealth();
      if (healthy) break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (!healthy) throw new Error("The local bridge did not answer GET /health.");
    setDaemonState("up");
    setStage(2, "done", "healthy");
    setStage(3, "active", "renderer");
    restartAttempts = 0;
    createTray();
    createMainWindow();
    connectSocket();
    startHealthLoop();
  } catch (error) {
    startup.error = error.message || String(error);
    log(`startup failed: ${startup.error}`);
    setDaemonState("down");
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.webContents.send("splash:state", {
        stages: startup.stages,
        progress: startup.progress,
        error: startup.error
      });
    }
  }
}

async function quitApplication() {
  if (quitting) return;
  quitting = true;
  stopHealthLoop();
  closeSocket();
  await stopDaemon();
  app.quit();
}

// ------------------------------------------------------------------------ ipc
ipcMain.handle("bridge:request", async (_event, payload) => {
  const method = String(payload?.method || "GET").toUpperCase();
  const requestPath = String(payload?.path || "/");
  if (!requestPath.startsWith("/api/") && requestPath !== "/health") {
    return { ok: false, status: 400, data: { detail: { code: "invalid_path", message: "Only local API paths are proxied." } } };
  }
  return bridgeRequest(method, requestPath, payload?.body);
});

ipcMain.handle("shell:info", () => ({
  version: app.getVersion(),
  electron: process.versions.electron,
  chrome: process.versions.chrome,
  node: process.versions.node,
  platform: process.platform,
  arch: process.arch,
  packaged: IS_PACKAGED,
  daemonState,
  daemonPort: daemonInfo?.port || null,
  daemonPid: daemonInfo?.pid || null,
  logPath: LOG_PATH,
  autostart: autostartState(),
  trayAvailable,
  projectRoot: PROJECT_ROOT,
  frontend: FRONTEND_DIST,
  capture: Boolean(CAPTURE_DIR),
  windowVisible: Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible())
}));

ipcMain.handle("shell:autostart", () => autostartState());
ipcMain.handle("shell:set-autostart", (_event, enabled) => {
  const result = setAutostart(Boolean(enabled));
  updateTray();
  return result;
});
ipcMain.handle("shell:open-log", () => shell.openPath(LOG_PATH));
ipcMain.handle("shell:quit", () => {
  void quitApplication();
  return true;
});
ipcMain.handle("shell:hide", () => {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.hide();
  return true;
});

ipcMain.on("splash:retry", () => {
  if (starting) return;
  restartAttempts = 0;
  for (let index = 0; index < startup.stages.length; index += 1) {
    startup.stages[index] = { ...startup.stages[index], state: "pending", detail: "" };
  }
  startup.error = "";
  startup.progress = 0;
  void boot();
});
ipcMain.on("splash:quit", () => void quitApplication());

// ---------------------------------------------------------------------- ready
app.whenReady().then(async () => {
  app.setName("ForgeMesh Client Manager");
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
    // The renderer must never reach the network directly. Everything goes
    // through IPC so the daemon's random token stays out of JavaScript.
    const devOrigin = DEV_URL ? new URL(DEV_URL).origin : "";
    if (/^(https?|wss?):/.test(details.url) && (!devOrigin || !details.url.startsWith(devOrigin))) {
      callback({ cancel: true });
      return;
    }
    callback({ cancel: false });
  });
  ensureDirs();
  log(`app ready packaged=${IS_PACKAGED} electron=${process.versions.electron}`);
  await boot();
  app.on("activate", () => showMainWindow());
});

process.on("uncaughtException", (error) => log(`uncaught: ${error.stack || error.message}`));
process.on("unhandledRejection", (error) => log(`unhandled rejection: ${error && error.stack ? error.stack : String(error)}`));

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    void quitApplication();
  });
}
