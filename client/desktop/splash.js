"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("fmcmSplash", {
  onState: (callback) => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on("splash:state", listener);
    return () => ipcRenderer.removeListener("splash:state", listener);
  },
  retry: () => ipcRenderer.send("splash:retry"),
  quit: () => ipcRenderer.send("splash:quit")
});
