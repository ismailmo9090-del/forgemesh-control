"use strict";

(function () {
  const stagesNode = document.getElementById("stages");
  const bar = document.getElementById("bar");
  const log = document.getElementById("log");
  const errorNode = document.getElementById("error");
  const actions = document.getElementById("actions");
  const retry = document.getElementById("retry");
  const quit = document.getElementById("quit");

  function render(state) {
    stagesNode.replaceChildren();
    for (const stage of state.stages || []) {
      const row = document.createElement("div");
      row.className = `stage ${stage.state || "pending"}`;
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.textContent = stage.state === "done" ? "✓" : stage.state === "failed" ? "!" : stage.state === "active" ? "…" : "";
      const label = document.createElement("span");
      label.textContent = stage.label;
      const detail = document.createElement("span");
      detail.className = "detail";
      detail.textContent = stage.detail || "";
      row.append(dot, label, detail);
      stagesNode.append(row);
      if (stage.state === "active" && stage.label) log.textContent = `${stage.label} — ${stage.detail || "working"}`;
    }
    bar.style.width = `${state.progress || 0}%`;
    errorNode.textContent = state.error || "";
    actions.classList.toggle("show", Boolean(state.error));
  }

  retry.addEventListener("click", () => {
    actions.classList.remove("show");
    errorNode.textContent = "";
    window.fmcmSplash.retry();
  });
  quit.addEventListener("click", () => window.fmcmSplash.quit());
  window.fmcmSplash.onState(render);
})();
