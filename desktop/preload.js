"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("forgemeshDesktop", {
  info: () => ipcRenderer.invoke("shell:info"),
  autostart: () => ipcRenderer.invoke("shell:autostart"),
  setAutostart: (enabled) => ipcRenderer.invoke("shell:set-autostart", enabled),
  openLog: () => ipcRenderer.invoke("shell:open-log"),
  restart: () => ipcRenderer.invoke("shell:restart"),
  hide: () => ipcRenderer.invoke("shell:hide"),
  closeWindow: () => ipcRenderer.invoke("shell:close-window"),
  onStartupState: (callback) => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on("startup:state", listener);
    return () => ipcRenderer.removeListener("startup:state", listener);
  },
  onStartupLog: (callback) => {
    const listener = (_event, text) => callback(text);
    ipcRenderer.on("startup:log", listener);
    return () => ipcRenderer.removeListener("startup:log", listener);
  },
  onServerRestarting: (callback) => {
    const listener = () => callback();
    ipcRenderer.on("shell:server-restarting", listener);
    return () => ipcRenderer.removeListener("shell:server-restarting", listener);
  },
  retry: () => ipcRenderer.send("startup:retry"),
  quit: () => ipcRenderer.send("app:quit")
});
