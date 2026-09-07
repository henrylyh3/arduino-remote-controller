const state = {
  nodes: [],
  devices: [],
  buttons: [],
  ac_controllers: [],
  timers: [],
  schedules: [],
  workflows: [],
  workflow_steps: [],
  workflow_schedules: [],
  workflow_runs: [],
  workflow_run_steps: [],
  events: [],
  timezone: "",
};

const dayNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let editingWorkflowId = null;
let selectedActionNodeId = null;
let selectedConfigNodeId = null;
let draggedNodeId = null;
let activeCapture = null;
let pendingCapturedSignal = null;
let activeAcControllerId = null;

const el = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function actionIcon(name) {
  const paths = {
    pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.623l4.352-1.321a2 2 0 0 0 .83-.5z"></path><path d="m15 5 4 4"></path>',
    trash: '<path d="M3 6h18"></path><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><path d="M10 11v6"></path><path d="M14 11v6"></path>',
  };
  return `<svg class="button-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${paths[name]}</svg>`;
}

function showToast(message, isError = false, durationMs = 3200) {
  const toast = el("toast");
  toast.textContent = message;
  toast.hidden = false;
  toast.style.background = isError ? "#7a1e1e" : "#17201b";
  toast.setAttribute("role", isError ? "alert" : "status");
  clearTimeout(showToast.timeout);
  if (durationMs > 0) {
    showToast.timeout = setTimeout(() => {
      toast.hidden = true;
    }, durationMs);
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }
  return response.json();
}

function optionList(items, getLabel, getValue = (item) => item.id) {
  if (!items.length) return '<option value="">None configured</option>';
  return items
    .map((item) => `<option value="${escapeHtml(getValue(item))}">${escapeHtml(getLabel(item))}</option>`)
    .join("");
}

function buttonContext(button) {
  const device = state.devices.find((item) => item.id === button.device_id);
  const node = state.nodes.find((item) => item.id === device?.node_id);
  return { device, node };
}

function buttonLabel(buttonId) {
  const button = state.buttons.find((item) => item.id === Number(buttonId));
  if (!button) return "Missing signal";
  const { node } = buttonContext(button);
  return `${node?.name || "Node"} / ${button.name}`;
}

function workflowLabel(workflowId) {
  const workflow = state.workflows.find((item) => item.id === Number(workflowId));
  return workflow?.name || "Missing workflow";
}

function workflowSteps(workflowId) {
  return state.workflow_steps
    .filter((step) => step.workflow_id === Number(workflowId))
    .sort((a, b) => a.step_order - b.step_order);
}

function groupSignalsByNode(buttons) {
  return state.nodes
    .map((node) => {
      const nodeButtons = buttons.filter((button) => buttonContext(button).node?.id === node.id);
      return { node, buttons: nodeButtons };
    })
    .filter((group) => group.buttons.length);
}

function controlSignalGroups(buttons) {
  return state.nodes
    .map((node) => ({
      node,
      buttons: buttons.filter((button) => buttonContext(button).node?.id === node.id),
      controller: state.ac_controllers.find((item) => item.node_id === node.id) || null,
    }))
    .filter((group) => group.buttons.length || group.controller);
}

function nodeTabs(groups, selectedNodeId, dataAttribute, label, disableInactive = false) {
  return `
    <div class="node-tabs" role="tablist" aria-label="${label}">
      ${groups
        .map((group) => {
          const disabled = disableInactive && group.node.health?.status !== "online";
          return `
            <button
              class="node-tab${group.node.id === selectedNodeId ? " is-active" : ""}"
              type="button"
              role="tab"
              aria-selected="${group.node.id === selectedNodeId}"
              ${dataAttribute}="${group.node.id}"
              ${disabled ? "disabled" : ""}
            >${nodeHealthView(group.node)}<span>${escapeHtml(group.node.name)}</span></button>
          `;
        })
        .join("")}
    </div>
  `;
}

function actions() {
  const signals = state.buttons.map((button) => ({
    kind: "signal",
    id: button.id,
    value: `signal:${button.id}`,
    name: button.name,
    label: `Signal - ${buttonLabel(button.id)}`,
    starred: button.starred,
  }));
  const workflows = state.workflows.map((workflow) => ({
    kind: "workflow",
    id: workflow.id,
    value: `workflow:${workflow.id}`,
    name: workflow.name,
    label: `Workflow - ${workflow.name}`,
    starred: workflow.starred,
  }));
  return [...signals, ...workflows];
}

function parseTarget(value) {
  const [kind, rawId] = String(value || "").split(":");
  const id = Number(rawId);
  if (!id || !["signal", "workflow"].includes(kind)) {
    throw new Error("Choose an action first");
  }
  return { kind, id };
}

function setSelectOptions(select, html) {
  const selected = select.value;
  select.innerHTML = html;
  if ([...select.options].some((option) => option.value === selected)) {
    select.value = selected;
  }
}

function updateSelectors() {
  setSelectOptions(el("captureNode"), optionList(state.nodes, (node) => node.name));
  const actionOptions = optionList(actions(), (action) => action.label, (action) => action.value);
  setSelectOptions(el("timerTarget"), actionOptions);
  setSelectOptions(el("scheduleTarget"), actionOptions);
  syncWorkflowStepOptions();
}

async function runAction(kind, id) {
  if (kind === "signal") {
    return api(`/api/buttons/${id}/press`, { method: "POST" });
  }
  return api(`/api/workflows/${id}/run`, { method: "POST", body: "{}" });
}

function renderActions() {
  const grid = el("actionGrid");
  const starredWorkflows = state.workflows.filter((workflow) => workflow.starred);
  const starredSignals = state.buttons.filter((button) => button.starred);
  const signalGroups = controlSignalGroups(starredSignals);
  if (!starredWorkflows.length && !signalGroups.length) {
    grid.innerHTML = '<div class="empty">No starred actions. Star one under Configuration.</div>';
    return;
  }

  const onlineSignalGroups = signalGroups.filter((group) => group.node.health?.status === "online");
  if (!onlineSignalGroups.some((group) => group.node.id === selectedActionNodeId)) {
    selectedActionNodeId = onlineSignalGroups[0]?.node.id || null;
  }
  const selectedGroup = onlineSignalGroups.find((group) => group.node.id === selectedActionNodeId);
  const workflowHtml = starredWorkflows.length
    ? `
      <section class="action-section">
        <h3>Workflows</h3>
        <div class="action-tile-grid">
          ${starredWorkflows
            .map((workflow) => {
              const count = workflowSteps(workflow.id).length;
              return `
                <button class="button-tile workflow-tile" type="button" data-action-kind="workflow" data-action-id="${workflow.id}">
                  ${escapeHtml(workflow.name)}
                  <span>WORKFLOW - ${count} step${count === 1 ? "" : "s"}</span>
                </button>
              `;
            })
            .join("")}
        </div>
      </section>
    `
    : "";
  const signalsHtml = signalGroups.length
    ? `
      <section class="action-section">
        <h3>Signals</h3>
        ${nodeTabs(signalGroups, selectedActionNodeId, "data-action-node-tab", "Signal nodes", true)}
        <div class="action-tile-grid node-tab-content" role="tabpanel">
          ${selectedGroup ? `
            ${selectedGroup.controller ? `
              <button class="button-tile ac-controller-tile" type="button" data-ac-controller-id="${selectedGroup.controller.id}">
                ${escapeHtml(selectedGroup.controller.name)}
                <span>${selectedGroup.controller.temperature}&deg;C &middot; Fan ${escapeHtml(selectedGroup.controller.fan === "auto" ? "Auto" : selectedGroup.controller.fan)} &middot; Swing ${selectedGroup.controller.swing ? "on" : "off"}</span>
              </button>
            ` : ""}
            ${selectedGroup.buttons.map((button) => {
              const count = button.stats?.press_count || 0;
              return `
                <button class="button-tile" type="button" data-action-kind="signal" data-action-id="${button.id}">
                  ${escapeHtml(button.name)}
                  <span>${escapeHtml(button.signal_type.toUpperCase())} - ${count} sent</span>
                </button>
              `;
            })
            .join("")}
          ` : '<div class="empty">No online signal nodes.</div>'}
        </div>
      </section>
    `
    : "";
  grid.innerHTML = workflowHtml + signalsHtml;

  grid.querySelectorAll("[data-action-node-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedActionNodeId = Number(button.dataset.actionNodeTab);
      renderActions();
    });
  });

  grid.querySelectorAll("[data-ac-controller-id]").forEach((button) => {
    button.addEventListener("click", () => openAcController(Number(button.dataset.acControllerId)));
  });

  grid.querySelectorAll("[data-action-kind]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await runAction(button.dataset.actionKind, Number(button.dataset.actionId));
        showToast(result.message || `${button.textContent.trim().split("\n")[0]} started`);
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  });
}

function activeAcController() {
  return state.ac_controllers.find((controller) => controller.id === activeAcControllerId) || null;
}

function fanLabel(fan) {
  return fan === "auto" ? "Auto" : fan;
}

function renderAcController() {
  const controller = activeAcController();
  if (!controller) return;
  el("acTemperatureDisplay").innerHTML = `${controller.temperature}&deg;`;
  el("acTemperatureValue").innerHTML = `${controller.temperature}&deg;C`;
  el("acDisplaySummary").textContent = `Fan ${fanLabel(controller.fan)} · Swing ${controller.swing ? "on" : "off"}`;
  el("decreaseAcTemperature").disabled = controller.temperature <= 16;
  el("increaseAcTemperature").disabled = controller.temperature >= 30;
  el("acSwing").checked = controller.swing;
  document.querySelectorAll("[data-ac-fan]").forEach((button) => {
    const selected = button.dataset.acFan === controller.fan;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  el("acLastSent").textContent = controller.last_sent_at
    ? `Last sent: ${controller.last_command} · ${new Date(controller.last_sent_at).toLocaleString()}`
    : "No command sent yet";
}

function openAcController(controllerId) {
  activeAcControllerId = controllerId;
  renderAcController();
  el("acControllerDialog").showModal();
}

function setAcControllerBusy(busy) {
  const dialog = el("acControllerDialog");
  dialog.setAttribute("aria-busy", String(busy));
  dialog.querySelectorAll("button, input").forEach((control) => {
    control.disabled = busy;
  });
  if (!busy) renderAcController();
}

async function sendAcController(changes = {}, powerToggle = false) {
  const controller = activeAcController();
  if (!controller) return;
  const command = {
    temperature: controller.temperature,
    fan: controller.fan,
    swing: controller.swing,
    ...changes,
    power_toggle: powerToggle,
  };
  setAcControllerBusy(true);
  try {
    const result = await api(`/api/ac-controllers/${controller.id}/send`, {
      method: "POST",
      body: JSON.stringify(command),
    });
    const index = state.ac_controllers.findIndex((item) => item.id === controller.id);
    state.ac_controllers[index] = result.controller;
    renderAcController();
    renderActions();
    showToast(result.message);
  } catch (error) {
    renderAcController();
    showToast(error.message, true);
  } finally {
    setAcControllerBusy(false);
  }
}

function daysLabel(days) {
  if (days.length === 7) return "daily";
  return days.map((day) => dayNames[day]).join(", ");
}

function formatDelay(seconds) {
  if (seconds === 0) return "immediate";
  if (seconds % 3600 === 0) {
    const hours = seconds / 3600;
    return `${hours} hr${hours === 1 ? "" : "s"}`;
  }
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} sec`;
}

function delayToSeconds(value, unit, allowZero = false) {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0 || (!allowZero && amount === 0)) {
    throw new Error(allowZero ? "Delay must be zero or positive" : "Delay must be positive");
  }
  const seconds = amount === 0 ? 0 : Math.max(1, Math.round(amount * unit));
  if (seconds > 7 * 24 * 60 * 60) throw new Error("Delay cannot exceed 7 days");
  return seconds;
}

function timerMinuteValues() {
  const values = [];
  for (let value = 0; value <= 180; value += 1) values.push(value);
  for (let value = 185; value <= 720; value += 5) values.push(value);
  for (let value = 750; value <= 1440; value += 30) values.push(value);
  for (let value = 1500; value <= 10080; value += 60) values.push(value);
  return values;
}

function setupTimerDurationPicker() {
  el("timerMinutes").innerHTML = timerMinuteValues()
    .map((value) => `<option value="${value}"${value === 30 ? " selected" : ""}>${value}</option>`)
    .join("");
  el("timerSeconds").innerHTML = Array.from({ length: 60 }, (_, value) =>
    `<option value="${value}">${String(value).padStart(2, "0")}</option>`,
  ).join("");
}

function starButton(actionType, actionId, starred) {
  const label = starred ? "Remove from Actions" : "Show in Actions";
  return `
    <button
      class="star-button${starred ? " is-starred" : ""}"
      type="button"
      data-star-type="${actionType}"
      data-star-id="${actionId}"
      data-starred="${starred}"
      aria-label="${label}"
      aria-pressed="${starred}"
      title="${label}"
    >${starred ? "&#9733;" : "&#9734;"}</button>
  `;
}

function bindStarButtons(container) {
  container.querySelectorAll("[data-star-type]").forEach((button) => {
    button.addEventListener("click", async () => {
      const starred = button.dataset.starred !== "true";
      try {
        await api(`/api/actions/${button.dataset.starType}/${button.dataset.starId}/star`, {
          method: "PUT",
          body: JSON.stringify({ starred }),
        });
        showToast(starred ? "Added to Actions" : "Removed from Actions");
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });
}

function nodeHealthView(node) {
  const status = ["online", "offline", "disabled"].includes(node.health?.status)
    ? node.health.status
    : "unknown";
  const labels = { online: "Online", offline: "Offline", disabled: "Disabled", unknown: "Checking" };
  const details = [labels[status]];
  if (node.health?.latency_ms !== null && node.health?.latency_ms !== undefined) {
    details.push(`${node.health.latency_ms} ms`);
  }
  if (node.health?.checked_at) details.push(`checked ${node.health.checked_at}`);
  if (node.health?.error) details.push(node.health.error);
  return `
    <span class="node-health node-health-${status}" aria-label="Node ${labels[status]}" title="${escapeHtml(details.join(" - "))}">
      <span class="node-status-dot" aria-hidden="true"></span>
    </span>
  `;
}

async function saveNodeOrder(nodeIds) {
  await api("/api/nodes/order", {
    method: "PUT",
    body: JSON.stringify({ node_ids: nodeIds }),
  });
  await loadState();
}

function renderNodeConfiguration() {
  const target = el("nodeList");
  if (document.activeElement?.matches("#nodeList input") || draggedNodeId !== null) return;
  if (!state.nodes.length) {
    target.innerHTML = '<div class="empty">No nodes.</div>';
    return;
  }

  target.innerHTML = state.nodes
    .map((node) => {
      const signalCount = state.buttons.filter((button) => buttonContext(button).node?.id === node.id).length;
      return `
        <div class="node-config-row" draggable="true" data-node-row="${node.id}">
          <span class="drag-handle" draggable="true" aria-hidden="true" title="Drag to reorder">&#8942;&#8942;</span>
          <div class="node-config-fields">
            <input
              id="node-name-${node.id}"
              aria-label="Node name"
              value="${escapeHtml(node.name)}"
              data-original-name="${escapeHtml(node.name)}"
              maxlength="80"
              autocomplete="off"
              draggable="false"
            />
            <div class="node-meta">
              <span class="muted">${escapeHtml(node.base_url.replace(/^https?:\/\//, ""))} - ${signalCount} signal${signalCount === 1 ? "" : "s"}</span>
              ${nodeHealthView(node)}
            </div>
          </div>
          <div class="row-actions node-row-actions">
            <button class="secondary" type="button" data-save-node="${node.id}" hidden>Save</button>
            <button class="danger icon-button" type="button" data-delete-node="${node.id}" aria-label="Delete ${escapeHtml(node.name)}" title="Delete node">${actionIcon("trash")}</button>
          </div>
        </div>
      `;
    })
    .join("");

  target.querySelectorAll("input[data-original-name]").forEach((input) => {
    input.addEventListener("input", () => {
      const id = input.id.replace("node-name-", "");
      target.querySelector(`[data-save-node="${id}"]`).hidden = input.value.trim() === input.dataset.originalName;
    });
  });

  target.querySelectorAll("[data-save-node]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = Number(button.dataset.saveNode);
      const name = el(`node-name-${id}`).value.trim();
      try {
        if (!name) throw new Error("Node name is required");
        await api(`/api/nodes/${id}`, { method: "PUT", body: JSON.stringify({ name }) });
        showToast("Node updated");
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });

  target.querySelectorAll("[data-delete-node]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = Number(button.dataset.deleteNode);
      const node = state.nodes.find((item) => item.id === id);
      const signalCount = state.buttons.filter((item) => buttonContext(item).node?.id === id).length;
      if (!window.confirm(`Delete node "${node?.name || "Unknown"}" and its ${signalCount} signal${signalCount === 1 ? "" : "s"}?`)) return;
      try {
        await api(`/api/nodes/${id}`, { method: "DELETE" });
        showToast("Node deleted");
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });

  target.querySelectorAll("[data-node-row]").forEach((row) => {
    row.addEventListener("dragstart", (event) => {
      draggedNodeId = Number(row.dataset.nodeRow);
      row.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(draggedNodeId));
    });
    row.addEventListener("dragover", (event) => {
      if (draggedNodeId === null || draggedNodeId === Number(row.dataset.nodeRow)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      row.classList.add("is-drop-target");
    });
    row.addEventListener("dragleave", () => row.classList.remove("is-drop-target"));
    row.addEventListener("drop", async (event) => {
      event.preventDefault();
      const targetId = Number(row.dataset.nodeRow);
      if (draggedNodeId === null || draggedNodeId === targetId) return;
      const movedNodeId = draggedNodeId;
      const nodeIds = state.nodes.map((node) => node.id).filter((id) => id !== movedNodeId);
      const targetIndex = nodeIds.indexOf(targetId);
      const insertAfter = event.clientY > row.getBoundingClientRect().top + row.getBoundingClientRect().height / 2;
      nodeIds.splice(targetIndex + (insertAfter ? 1 : 0), 0, movedNodeId);
      draggedNodeId = null;
      try {
        await saveNodeOrder(nodeIds);
      } catch (error) {
        showToast(error.message, true);
      }
    });
    row.addEventListener("dragend", () => {
      draggedNodeId = null;
      target.querySelectorAll(".is-dragging, .is-drop-target").forEach((item) => {
        item.classList.remove("is-dragging", "is-drop-target");
      });
    });
  });
}

function runProgress(runId) {
  const steps = state.workflow_run_steps.filter((step) => step.run_id === runId);
  const completed = steps.filter((step) => step.status === "done").length;
  const next = steps
    .filter((step) => ["waiting", "pending", "running"].includes(step.status))
    .sort((a, b) => a.step_order - b.step_order)[0];
  return { steps, completed, next };
}

function renderActiveJobs() {
  const target = el("activeJobList");
  const jobs = [];

  state.timers
    .filter((timer) => ["pending", "running"].includes(timer.status))
    .forEach((timer) => {
      jobs.push({
        type: "Timer",
        name: timer.name,
        detail: `${buttonLabel(timer.button_id)} - ${timer.status} - ${timer.run_at_utc}`,
        cancelPath: `/api/timers/${timer.id}/cancel`,
      });
    });

  state.schedules
    .filter((schedule) => schedule.enabled)
    .forEach((schedule) => {
      jobs.push({
        type: "Schedule",
        name: schedule.name,
        detail: `${buttonLabel(schedule.button_id)} - ${schedule.time_of_day} - ${daysLabel(schedule.days)}`,
      });
    });

  state.workflow_schedules
    .filter((schedule) => schedule.enabled)
    .forEach((schedule) => {
      jobs.push({
        type: "Schedule",
        name: schedule.name,
        detail: `${workflowLabel(schedule.workflow_id)} - ${schedule.time_of_day} - ${daysLabel(schedule.days)}`,
      });
    });

  state.workflow_runs
    .filter((run) => ["pending", "running"].includes(run.status))
    .forEach((run) => {
      const progress = runProgress(run.id);
      const nextLabel = progress.next
        ? `next: ${buttonLabel(progress.next.button_id)}${progress.next.run_after_utc ? ` at ${progress.next.run_after_utc}` : ""}`
        : "waiting to finish";
      jobs.push({
        type: "Workflow run",
        name: run.name,
        detail: `${progress.completed} of ${progress.steps.length} completed - ${nextLabel}`,
        cancelPath: `/api/workflow-runs/${run.id}/cancel`,
      });
    });

  if (!jobs.length) {
    target.innerHTML = '<div class="empty">No active jobs.</div>';
    return;
  }

  target.innerHTML = jobs
    .map((job, index) => `
      <div class="row">
        <div>
          <strong>${escapeHtml(job.name)}</strong>
          <div class="muted">${escapeHtml(job.type)}</div>
          <span>${escapeHtml(job.detail)}</span>
        </div>
        ${job.cancelPath ? `<button class="secondary" type="button" data-cancel-job="${index}">Cancel</button>` : ""}
      </div>
    `)
    .join("");

  document.querySelectorAll("[data-cancel-job]").forEach((button) => {
    button.addEventListener("click", async () => {
      const job = jobs[Number(button.dataset.cancelJob)];
      try {
        await api(job.cancelPath, { method: "POST" });
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });
}

function renderSchedules() {
  const target = el("scheduleList");
  const schedules = [
    ...state.schedules.map((schedule) => ({ ...schedule, kind: "signal", targetLabel: buttonLabel(schedule.button_id) })),
    ...state.workflow_schedules.map((schedule) => ({ ...schedule, kind: "workflow", targetLabel: workflowLabel(schedule.workflow_id) })),
  ].sort((a, b) => a.time_of_day.localeCompare(b.time_of_day) || a.name.localeCompare(b.name));

  if (!schedules.length) {
    target.innerHTML = '<div class="empty">No schedules.</div>';
    return;
  }

  target.innerHTML = schedules
    .map((schedule) => `
      <div class="row">
        <div>
          <strong>${escapeHtml(schedule.name)}</strong>
          <div class="muted">
            ${escapeHtml(schedule.targetLabel)} - ${escapeHtml(schedule.time_of_day)} -
            ${escapeHtml(daysLabel(schedule.days))} - ${schedule.enabled ? "enabled" : "paused"}
          </div>
        </div>
        <button class="secondary" type="button" data-toggle-schedule="${schedule.id}" data-schedule-kind="${schedule.kind}">
          ${schedule.enabled ? "Pause" : "Enable"}
        </button>
      </div>
    `)
    .join("");

  document.querySelectorAll("[data-toggle-schedule]").forEach((button) => {
    button.addEventListener("click", async () => {
      const root = button.dataset.scheduleKind === "workflow" ? "workflow-schedules" : "schedules";
      try {
        await api(`/api/${root}/${button.dataset.toggleSchedule}/toggle`, { method: "POST" });
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });
}

function renderSignalConfiguration() {
  const target = el("signalConfigList");
  if (document.activeElement?.matches("#signalConfigList input")) return;
  if (!state.buttons.length) {
    target.innerHTML = '<div class="empty">No learned signals.</div>';
    return;
  }

  const signalGroups = groupSignalsByNode(state.buttons);
  if (!signalGroups.some((group) => group.node.id === selectedConfigNodeId)) {
    selectedConfigNodeId = signalGroups[0].node.id;
  }
  const selectedGroup = signalGroups.find((group) => group.node.id === selectedConfigNodeId);
  target.innerHTML = `
    ${nodeTabs(signalGroups, selectedConfigNodeId, "data-config-node-tab", "Configuration signal nodes")}
    <div class="node-tab-content" role="tabpanel">
      ${selectedGroup.buttons
    .map((button) => {
      const { node } = buttonContext(button);
      return `
        <div class="row signal-config-row">
          <div class="signal-config-fields">
            <label for="signal-name-${button.id}">Name</label>
            <input id="signal-name-${button.id}" value="${escapeHtml(button.name)}" data-original-name="${escapeHtml(button.name)}" maxlength="80" autocomplete="off" />
            <span class="muted">${escapeHtml(button.signal_type.toUpperCase())} - ${escapeHtml(node?.name || "Node")}</span>
          </div>
          <div class="row-actions">
            ${starButton("signal", button.id, button.starred)}
            <button class="secondary" type="button" data-save-signal="${button.id}" hidden>Save</button>
            <button class="danger icon-button" type="button" data-delete-signal="${button.id}" aria-label="Delete ${escapeHtml(button.name)}" title="Delete signal">${actionIcon("trash")}</button>
          </div>
        </div>
      `;
    })
    .join("")}
    </div>
  `;

  target.querySelectorAll("[data-config-node-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedConfigNodeId = Number(button.dataset.configNodeTab);
      renderSignalConfiguration();
    });
  });

  target.querySelectorAll("input[data-original-name]").forEach((input) => {
    input.addEventListener("input", () => {
      const saveButton = target.querySelector(`[data-save-signal="${input.id.replace("signal-name-", "")}"]`);
      saveButton.hidden = input.value.trim() === input.dataset.originalName;
    });
  });

  target.querySelectorAll("[data-save-signal]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = Number(button.dataset.saveSignal);
      const name = el(`signal-name-${id}`).value.trim();
      try {
        if (!name) throw new Error("Signal name is required");
        await api(`/api/signals/${id}`, { method: "PUT", body: JSON.stringify({ name }) });
        showToast("Signal updated");
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });

  target.querySelectorAll("[data-delete-signal]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = Number(button.dataset.deleteSignal);
      const signal = state.buttons.find((item) => item.id === id);
      if (!window.confirm(`Delete signal "${signal?.name || "Unknown"}"?`)) return;
      try {
        await api(`/api/signals/${id}`, { method: "DELETE" });
        showToast("Signal deleted");
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });
  bindStarButtons(target);
}

function syncWorkflowStepOptions() {
  const options = optionList(state.buttons, (button) => buttonLabel(button.id));
  document.querySelectorAll("#workflowSteps select[name='button_id']").forEach((select) => {
    setSelectOptions(select, options);
  });
}

function addWorkflowStepRow(delay = 30, unit = 60, buttonId = "") {
  const row = document.createElement("div");
  row.className = "workflow-step";
  row.innerHTML = `
    <input name="delay" aria-label="Delay" type="number" min="0" max="10080" step="any" value="${delay}" required />
    <select name="unit" aria-label="Delay unit">
      <option value="60"${unit === 60 ? " selected" : ""}>minutes</option>
      <option value="3600"${unit === 3600 ? " selected" : ""}>hours</option>
    </select>
    <select name="button_id" aria-label="Signal" required></select>
    <button class="secondary icon-button" type="button" data-remove-step aria-label="Remove workflow step" title="Remove step">${actionIcon("trash")}</button>
  `;
  el("workflowSteps").appendChild(row);
  syncWorkflowStepOptions();
  if (buttonId) row.querySelector("select[name='button_id']").value = String(buttonId);
  row.querySelector("[data-remove-step]").addEventListener("click", () => {
    if (document.querySelectorAll(".workflow-step").length > 1) row.remove();
  });
}

function splitDelay(seconds) {
  if (seconds > 0 && seconds % 3600 === 0) return { delay: seconds / 3600, unit: 3600 };
  return { delay: Number((seconds / 60).toFixed(4)), unit: 60 };
}

function resetWorkflowForm() {
  editingWorkflowId = null;
  el("workflowForm").reset();
  el("workflowSteps").innerHTML = "";
  addWorkflowStepRow();
  el("saveWorkflowButton").textContent = "Save workflow";
  el("cancelWorkflowEdit").hidden = true;
}

function editWorkflow(workflowId) {
  const workflow = state.workflows.find((item) => item.id === Number(workflowId));
  if (!workflow) return;

  editingWorkflowId = workflow.id;
  el("workflowForm").querySelector("input[name='name']").value = workflow.name;
  el("workflowSteps").innerHTML = "";
  const steps = workflowSteps(workflow.id);
  steps.forEach((step) => {
    const { delay, unit } = splitDelay(step.delay_seconds);
    addWorkflowStepRow(delay, unit, step.button_id);
  });
  if (!steps.length) addWorkflowStepRow(0, 60);

  el("saveWorkflowButton").textContent = "Update workflow";
  el("cancelWorkflowEdit").hidden = false;
  el("workflowForm").scrollIntoView({ behavior: "smooth", block: "start" });
}

function workflowStepSummary(workflowId) {
  const steps = workflowSteps(workflowId);
  if (!steps.length) return '<div class="muted">No steps.</div>';
  return `
    <div class="step-summary">
      ${steps
        .map((step) => {
          const delay = step.delay_seconds === 0 ? "immediate" : `+${formatDelay(step.delay_seconds)}`;
          return `<span>${delay} -> ${escapeHtml(buttonLabel(step.button_id))}</span>`;
        })
        .join("")}
    </div>
  `;
}

function renderWorkflows() {
  const target = el("workflowList");
  if (!state.workflows.length) {
    target.innerHTML = '<div class="empty">No workflows.</div>';
    return;
  }

  target.innerHTML = state.workflows
    .map((workflow) => `
      <div class="row">
        <div>
          <strong>${escapeHtml(workflow.name)}</strong>
          ${workflowStepSummary(workflow.id)}
        </div>
        <div class="row-actions">
          ${starButton("workflow", workflow.id, workflow.starred)}
          <button class="secondary icon-button" type="button" data-edit-workflow="${workflow.id}" aria-label="Edit ${escapeHtml(workflow.name)}" title="Edit workflow">${actionIcon("pencil")}</button>
          <button class="danger icon-button" type="button" data-delete-workflow="${workflow.id}" aria-label="Delete ${escapeHtml(workflow.name)}" title="Delete workflow">${actionIcon("trash")}</button>
        </div>
      </div>
    `)
    .join("");

  document.querySelectorAll("[data-edit-workflow]").forEach((button) => {
    button.addEventListener("click", () => editWorkflow(button.dataset.editWorkflow));
  });
  document.querySelectorAll("[data-delete-workflow]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = Number(button.dataset.deleteWorkflow);
      const workflow = state.workflows.find((item) => item.id === id);
      if (!window.confirm(`Delete workflow "${workflow?.name || "Unknown"}", its schedules, and run history?`)) return;
      try {
        await api(`/api/workflows/${id}`, { method: "DELETE" });
        if (editingWorkflowId === id) resetWorkflowForm();
        showToast("Workflow deleted");
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });
  bindStarButtons(target);
}

function renderEvents() {
  const target = el("eventList");
  if (!state.events.length) {
    target.innerHTML = '<div class="empty">No events.</div>';
    return;
  }
  target.innerHTML = state.events
    .map((event) => `
      <div class="row">
        <div>
          <strong class="${event.status === "failed" ? "bad" : ""}">${escapeHtml(event.status)}</strong>
          <div class="muted">${escapeHtml(event.created_at)}</div>
          <span>${escapeHtml(event.message)}</span>
        </div>
      </div>
    `)
    .join("");
}

async function loadState() {
  try {
    const nextState = await api("/api/state");
    Object.assign(state, nextState);
    el("apiStatus").textContent = `${state.nodes.length} nodes - ${actions().length + state.ac_controllers.length} actions`;
    el("clock").textContent = state.timezone;
    updateSelectors();
    renderActions();
    renderActiveJobs();
    renderSchedules();
    renderNodeConfiguration();
    renderSignalConfiguration();
    renderWorkflows();
    renderEvents();
  } catch (error) {
    el("apiStatus").textContent = "Offline";
    showToast(error.message, true);
  }
}

function formJson(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function activateTab(name, updateHash = true) {
  const tabs = ["control", "configuration", "log"];
  const activeTab = tabs.includes(name) ? name : "control";
  tabs.forEach((tabName) => {
    const active = tabName === activeTab;
    el(`${tabName}Panel`).hidden = !active;
    el(`${tabName}Tab`).classList.toggle("is-active", active);
    el(`${tabName}Tab`).setAttribute("aria-selected", String(active));
  });
  if (updateHash) history.replaceState(null, "", `#${activeTab}`);
}

document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => activateTab(button.dataset.tab));
});

el("closeAcController").addEventListener("click", () => el("acControllerDialog").close());
el("acControllerDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
el("acControllerDialog").addEventListener("close", () => {
  activeAcControllerId = null;
});
el("decreaseAcTemperature").addEventListener("click", () => {
  const controller = activeAcController();
  if (controller) sendAcController({ temperature: Math.max(16, controller.temperature - 1) });
});
el("increaseAcTemperature").addEventListener("click", () => {
  const controller = activeAcController();
  if (controller) sendAcController({ temperature: Math.min(30, controller.temperature + 1) });
});
document.querySelectorAll("[data-ac-fan]").forEach((button) => {
  button.addEventListener("click", () => sendAcController({ fan: button.dataset.acFan }));
});
el("acSwing").addEventListener("change", (event) => {
  sendAcController({ swing: event.currentTarget.checked });
});
el("acPower").addEventListener("click", () => sendAcController({}, true));

el("refreshButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Checking...";
  try {
    const result = await api("/api/node-health/refresh", { method: "POST" });
    await loadState();
    showToast(`${result.online} of ${result.total} nodes online`);
  } catch (error) {
    showToast(`Health check failed: ${error.message}`, true);
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "Refresh";
  }
});
el("addWorkflowStep").addEventListener("click", () => addWorkflowStepRow());
el("cancelWorkflowEdit").addEventListener("click", resetWorkflowForm);

el("stopCapture").addEventListener("click", async () => {
  const capture = activeCapture;
  if (!capture) return;
  const stopButton = el("stopCapture");
  capture.stopped = true;
  stopButton.disabled = true;
  stopButton.textContent = "Stopping...";
  showToast("Stopping capture", false, 0);
  try {
    await api(`/api/nodes/${capture.nodeId}/cancel-capture`, { method: "POST" });
    capture.controller.abort();
  } catch (error) {
    capture.stopped = false;
    stopButton.disabled = false;
    stopButton.textContent = "Stop capture";
    showToast(`Unable to stop capture: ${error.message}`, true);
  }
});

function resetCapturedSignal() {
  pendingCapturedSignal = null;
  el("captureForm").elements.name.value = "";
  el("captureSetup").hidden = false;
  el("captureNaming").hidden = true;
}

el("discardCapture").addEventListener("click", resetCapturedSignal);

el("nodeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/nodes", { method: "POST", body: JSON.stringify(formJson(form)) });
    form.reset();
    showToast("Node saved");
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

el("startCapture").addEventListener("click", async () => {
  const form = el("captureForm");
  const body = formJson(form);
  const timeoutMs = Number(body.timeout || 8) * 1000;
  const captureButton = el("startCapture");
  const stopButton = el("stopCapture");
  const capture = {
    nodeId: Number(body.node_id),
    controller: new AbortController(),
    stopped: false,
  };
  const captureDeadline = Date.now() + timeoutMs;
  const signalLabel = body.signal_type === "ir" ? "IR" : "RF";
  const updateCaptureMessage = () => {
    const secondsRemaining = Math.max(0, Math.ceil((captureDeadline - Date.now()) / 1000));
    showToast(`Capturing ${signalLabel} signal - ${secondsRemaining > 0 ? `${secondsRemaining}s remaining` : "finishing"}`, false, 0);
  };
  let captureCountdown;
  try {
    if (!Number(body.node_id)) throw new Error("Add a node first");
    captureButton.disabled = true;
    captureButton.classList.add("is-loading");
    captureButton.textContent = "Sensing...";
    stopButton.hidden = false;
    stopButton.disabled = false;
    activeCapture = capture;
    form.setAttribute("aria-busy", "true");
    updateCaptureMessage();
    captureCountdown = setInterval(updateCaptureMessage, 1000);
    const result = await api(
      `/api/nodes/${Number(body.node_id)}/capture/${body.signal_type}?timeout_ms=${timeoutMs}`,
      {
      method: "POST",
      signal: capture.controller.signal,
      },
    );
    clearInterval(captureCountdown);
    if (result.duplicate) {
      showToast(`Signal received. Already saved as ${result.existing.name}`);
    } else {
      pendingCapturedSignal = {
        node_id: Number(body.node_id),
        signal_type: body.signal_type,
        payload: result.payload,
      };
      el("captureSetup").hidden = true;
      el("captureNaming").hidden = false;
      form.elements.name.focus();
      showToast("Signal detected. Give it a name");
    }
  } catch (error) {
    if (capture.stopped || error.name === "AbortError") {
      showToast("Capture stopped");
    } else {
      showToast(`Capture failed: ${error.message}`, true);
    }
  } finally {
    clearInterval(captureCountdown);
    captureButton.disabled = false;
    captureButton.classList.remove("is-loading");
    captureButton.textContent = "Sense signal";
    stopButton.hidden = true;
    stopButton.disabled = false;
    stopButton.textContent = "Stop capture";
    if (activeCapture === capture) activeCapture = null;
    form.removeAttribute("aria-busy");
  }
});

el("captureForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!pendingCapturedSignal) return;
  const form = event.currentTarget;
  const name = form.elements.name.value.trim();
  if (!name) {
    form.elements.name.focus();
    showToast("Enter a signal name", true);
    return;
  }
  const saveButton = form.querySelector('#captureNaming button[type="submit"]');
  try {
    saveButton.disabled = true;
    const result = await api("/api/signals/save", {
      method: "POST",
      body: JSON.stringify({ ...pendingCapturedSignal, name }),
    });
    if (result.duplicate) {
      showToast(`Already saved as ${result.existing.name}`);
    } else {
      showToast(`Saved ${result.name}`);
    }
    resetCapturedSignal();
    await loadState();
  } catch (error) {
    showToast(`Unable to save signal: ${error.message}`, true);
  } finally {
    saveButton.disabled = false;
  }
});

el("timerForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  try {
    const target = parseTarget(data.get("target"));
    const useMobilePicker = window.matchMedia("(max-width: 620px)").matches;
    const seconds = useMobilePicker
      ? delayToSeconds(
          Number(data.get("mobile_minutes")) * 60 + Number(data.get("mobile_seconds")),
          1,
        )
      : delayToSeconds(data.get("minutes"), 60);
    const selected = actions().find((action) => action.value === data.get("target"));
    const name = `${selected?.name || "Action"} timer`;
    if (target.kind === "signal") {
      await api("/api/timers", {
        method: "POST",
        body: JSON.stringify({ button_id: target.id, seconds, name }),
      });
    } else {
      await api(`/api/workflows/${target.id}/run`, {
        method: "POST",
        body: JSON.stringify({ delay_seconds: seconds, name }),
      });
    }
    form.reset();
    showToast("Timer started");
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

el("workflowForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const rows = [...document.querySelectorAll(".workflow-step")];
  try {
    const steps = rows.map((row) => ({
      button_id: Number(row.querySelector("select[name='button_id']").value),
      delay_seconds: delayToSeconds(
        row.querySelector("input[name='delay']").value,
        Number(row.querySelector("select[name='unit']").value),
        true,
      ),
    }));
    if (!steps.length || steps.some((step) => !step.button_id)) {
      throw new Error("Add at least one workflow step with a signal");
    }
    const data = new FormData(form);
    const wasEditing = editingWorkflowId !== null;
    await api(editingWorkflowId ? `/api/workflows/${editingWorkflowId}` : "/api/workflows", {
      method: editingWorkflowId ? "PUT" : "POST",
      body: JSON.stringify({ name: data.get("name") || "Workflow", steps }),
    });
    resetWorkflowForm();
    showToast(wasEditing ? "Workflow updated" : "Workflow saved");
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

el("scheduleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  try {
    const target = parseTarget(data.get("target"));
    const selected = actions().find((action) => action.value === data.get("target"));
    const shared = {
      name: data.get("name") || `${selected?.name || "Action"} schedule`,
      time_of_day: data.get("time_of_day"),
      days: data.getAll("days").map(Number),
      enabled: true,
    };
    if (!shared.days.length) throw new Error("Choose at least one day");
    if (target.kind === "signal") {
      await api("/api/schedules", {
        method: "POST",
        body: JSON.stringify({ ...shared, button_id: target.id }),
      });
    } else {
      await api("/api/workflow-schedules", {
        method: "POST",
        body: JSON.stringify({ ...shared, workflow_id: target.id }),
      });
    }
    form.reset();
    showToast("Schedule saved");
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

activateTab(location.hash.slice(1), false);
setupTimerDurationPicker();
addWorkflowStepRow();
loadState();
setInterval(loadState, 5000);
