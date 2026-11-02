"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("forgemeshClient", {
  request: (method, path, body) => ipcRenderer.invoke("bridge:request", { method, path, body }),
  info: () => ipcRenderer.invoke("shell:info"),
  autostart: () => ipcRenderer.invoke("shell:autostart"),
  setAutostart: (enabled) => ipcRenderer.invoke("shell:set-autostart", enabled),
  openLog: () => ipcRenderer.invoke("shell:open-log"),
  quit: () => ipcRenderer.invoke("shell:quit"),
  hide: () => ipcRenderer.invoke("shell:hide"),
  onEvent: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("bridge:event", listener);
    return () => ipcRenderer.removeListener("bridge:event", listener);
  },
  onDaemonState: (callback) => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on("bridge:state", listener);
    return () => ipcRenderer.removeListener("bridge:state", listener);
  }
});
