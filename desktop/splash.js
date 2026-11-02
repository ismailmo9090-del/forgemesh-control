"use strict";

const stagesElement = document.getElementById("stages");
const progressElement = document.getElementById("progress");
const errorElement = document.getElementById("error");
const logElement = document.getElementById("log");
const metaElement = document.getElementById("meta");
const retryButton = document.getElementById("retry");
const quitButton = document.getElementById("quit");

const api = window.forgemeshDesktop;

function renderStages(stages) {
  stagesElement.innerHTML = "";
  for (const stage of stages) {
    const row = document.createElement("div");
    row.className = `stage ${stage.state}`;
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.textContent = stage.state === "done" ? "✓" : stage.state === "failed" ? "!" : stage.state === "active" ? "◌" : "";
    const label = document.createElement("span");
    label.textContent = stage.label;
    const detail = document.createElement("span");
    detail.className = "detail";
    detail.textContent = stage.detail || "";
    row.append(dot, label, detail);
    stagesElement.append(row);
  }
  const done = stages.filter((stage) => stage.state === "done").length;
  progressElement.style.width = `${Math.round((done / Math.max(stages.length, 1)) * 100)}%`;
}

function render(state) {
  renderStages(state.stages || []);
  metaElement.textContent = `elapsed ${state.elapsed || "0.0s"}${
    state.serverMode ? ` · server ${state.serverMode}` : ""
  }`;
  if (state.error) {
    document.body.classList.add("failed");
    errorElement.textContent = state.error;
  } else {
    document.body.classList.remove("failed");
    errorElement.textContent = "";
  }
  if (state.log) logElement.textContent = state.log;
}

retryButton.addEventListener("click", () => {
  document.body.classList.remove("failed");
  errorElement.textContent = "";
  logElement.textContent = "Retrying…";
  api.retry();
});

quitButton.addEventListener("click", () => api.quit());

if (api) {
  api.onStartupState(render);
  api.onStartupLog((text) => {
    if (text) logElement.textContent = text;
  });
} else {
  errorElement.textContent = "The desktop preload bridge is unavailable.";
  document.body.classList.add("failed");
}
