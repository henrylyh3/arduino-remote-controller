const state = {
  nodes: [],
  devices: [],
  buttons: [],
  ac_controllers: [],
  timers: [],
  timer_presets: [],
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
let editingNodeId = null;
let selectedActionNodeId = null;
let selectedConfigNodeId = null;
let draggedNodeId = null;
let activeCapture = null;
let pendingCapturedSignal = null;
let activeAcControllerId = null;
let activeAcControllerNodeId = null;
let editingSchedule = null;
let editingTimerId = null;
let workflowStepKey = 0;
let eventPage = 1;
let eventTotalPages = 1;
const eventPageSize = 10;

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

function renderControllerTabs() {
  el("controllerTabs").innerHTML = state.ac_controllers
    .map((controller) => `
      <a class="tab" href="/${controller.slug}" role="tab" aria-selected="false">${escapeHtml(controller.name)}</a>
    `)
    .join("");
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
    }))
    .filter((group) => group.buttons.length);
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

function actionSelectOptions() {
  const groups = [];
  if (state.workflows.length) {
    groups.push(`
      <optgroup label="Workflows">
        ${state.workflows
          .map((workflow) => `<option value="workflow:${workflow.id}">${escapeHtml(workflow.name)}</option>`)
          .join("")}
      </optgroup>
    `);
  }
  state.nodes.forEach((node) => {
    const nodeSignals = state.buttons.filter((button) => buttonContext(button).node?.id === node.id);
    if (!nodeSignals.length) return;
    groups.push(`
      <optgroup label="${escapeHtml(node.name)} signals">
        ${nodeSignals
          .map((button) => `<option value="signal:${button.id}">${escapeHtml(button.name)}</option>`)
          .join("")}
      </optgroup>
    `);
  });
  return groups.length ? groups.join("") : '<option value="">None configured</option>';
}

function updateSelectors() {
  setSelectOptions(el("captureNode"), optionList(state.nodes, (node) => node.name));
  const actionOptions = actionSelectOptions();
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
  const controllers = state.ac_controllers;
  const starredWorkflows = state.workflows.filter((workflow) => workflow.starred);
  const starredSignals = state.buttons.filter((button) => button.starred);
  const signalGroups = controlSignalGroups(starredSignals);
  if (!controllers.length && !starredWorkflows.length && !signalGroups.length) {
    grid.innerHTML = '<div class="empty">No starred actions. Star one under Configuration.</div>';
    return;
  }

  const onlineSignalGroups = signalGroups.filter((group) => group.node.health?.status === "online");
  if (!onlineSignalGroups.some((group) => group.node.id === selectedActionNodeId)) {
    selectedActionNodeId = onlineSignalGroups[0]?.node.id || null;
  }
  const selectedGroup = onlineSignalGroups.find((group) => group.node.id === selectedActionNodeId);
  const controllersHtml = controllers.length
    ? `
      <section class="action-section">
        <h3>Controllers</h3>
        <div class="action-tile-grid">
          ${controllers
            .map((controller) => `
              <button class="button-tile ac-controller-tile" type="button" data-ac-controller-id="${controller.id}">
                ${escapeHtml(controller.name)}
                <span>${escapeHtml(controller.brand)} &middot; ${controller.power ? "On" : "Off"} &middot; ${controller.temperature}&deg;C &middot; Fan ${escapeHtml(fanLabel(controller.fan))} &middot; Swing ${controller.swing ? "Auto" : "Fixed"}</span>
              </button>
            `)
            .join("")}
        </div>
      </section>
    `
    : "";
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
          ` : '<div class="empty">No node(s) online.</div>'}
        </div>
      </section>
    `
    : "";
  grid.innerHTML = controllersHtml + workflowHtml + signalsHtml;

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

function activeAcControllerNode() {
  return state.nodes.find((node) => node.id === activeAcControllerNodeId) || null;
}

function acControllerNodeOptions() {
  if (!state.nodes.length) return '<option value="">No nodes configured</option>';
  return state.nodes
    .map((node) => {
      const status = node.health?.status || "unknown";
      const selected = node.id === activeAcControllerNodeId ? " selected" : "";
      return `<option value="${node.id}"${selected}>${escapeHtml(node.name)} - ${escapeHtml(status)}</option>`;
    })
    .join("");
}

function renderAcController() {
  const controller = activeAcController();
  if (!controller) return;
  const node = activeAcControllerNode();
  const status = node?.health?.status || "unknown";
  const canSend = Boolean(node?.enabled) && status === "online";
  el("acTemperatureDisplay").innerHTML = `${controller.temperature}&deg;`;
  el("acTemperatureValue").innerHTML = `${controller.temperature}&deg;C`;
  el("acControllerMeta").textContent = controller.brand;
  el("acNode").innerHTML = acControllerNodeOptions();
  el("acNodeStatus").className = `node-status-dot node-status-${status}`;
  el("acNodeField").setAttribute("aria-label", node ? `${node.name} ${status}` : "No node selected");
  el("acDisplaySummary").textContent = `${controller.power ? "ON" : "OFF"} · Fan ${fanLabel(controller.fan)} · Swing ${controller.swing ? "Auto" : "Fixed"}`;
  el("decreaseAcTemperature").disabled = !canSend || controller.temperature <= 16;
  el("increaseAcTemperature").disabled = !canSend || controller.temperature >= 30;
  el("acSwing").checked = controller.swing;
  el("acPowerLabel").textContent = controller.power ? "Turn off" : "Turn on";
  document.querySelectorAll("[data-ac-fan]").forEach((button) => {
    const selected = button.dataset.acFan === controller.fan;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
    button.disabled = !canSend;
  });
  el("acSwing").disabled = !canSend;
  el("acPower").disabled = !canSend;
  el("acLastSent").textContent = controller.last_sent_at
    ? `Last sent: ${controller.last_command} · ${new Date(controller.last_sent_at).toLocaleString()}`
    : "No command sent yet";
}

function openAcController(controllerId) {
  activeAcControllerId = controllerId;
  const controller = activeAcController();
  activeAcControllerNodeId = controller?.last_node_id
    || state.nodes.find((node) => node.health?.status === "online" && node.enabled)?.id
    || state.nodes[0]?.id
    || null;
  renderAcController();
  el("acControllerDialog").showModal();
}

function setAcControllerBusy(busy) {
  const dialog = el("acControllerDialog");
  dialog.setAttribute("aria-busy", String(busy));
  dialog.querySelectorAll("button, input, select").forEach((control) => {
    control.disabled = busy;
  });
  if (!busy) renderAcController();
}

async function sendAcController(changes = {}, powerToggle = false) {
  const controller = activeAcController();
  if (!controller) return;
  const command = {
    node_id: activeAcControllerNodeId,
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
    activeAcControllerNodeId = result.controller.last_node_id;
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
    return `${hours} hr`;
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

function rollerOption(value, format) {
  const option = document.createElement("button");
  option.className = "roller-option";
  option.type = "button";
  option.role = "option";
  option.dataset.value = String(value);
  option.textContent = format(value);
  return option;
}

function selectRollerValue(rollerId, value, notify = false) {
  const roller = el(rollerId);
  let options = [...roller.querySelectorAll(".roller-option")];
  let option = options.find((item) => Number(item.dataset.value) === Number(value));
  if (!option) {
    option = rollerOption(value, roller.valueFormatter || String);
    const nextOption = options.find((item) => Number(item.dataset.value) > Number(value));
    roller.insertBefore(option, nextOption || null);
    options = [...roller.querySelectorAll(".roller-option")];
  }
  const selectedIndex = options.indexOf(option);
  options.forEach((item, index) => {
    const selected = index === selectedIndex;
    item.classList.toggle("is-selected", selected);
    item.setAttribute("aria-selected", String(selected));
  });
  const input = el(roller.dataset.inputId);
  input.value = option.dataset.value;
  requestAnimationFrame(() => {
    const rowHeight = option.offsetHeight || 42;
    roller.scrollTo({ top: selectedIndex * rowHeight, behavior: "auto" });
  });
  if (notify) input.dispatchEvent(new Event("change", { bubbles: true }));
}

function setupRoller(rollerId, inputId, values, format = String) {
  const roller = el(rollerId);
  roller.dataset.inputId = inputId;
  roller.valueFormatter = format;
  values.forEach((value) => roller.appendChild(rollerOption(value, format)));
  roller.addEventListener("click", (event) => {
    const option = event.target.closest(".roller-option");
    if (option) selectRollerValue(rollerId, option.dataset.value, true);
  });
  roller.addEventListener("scroll", () => {
    clearTimeout(roller.scrollTimer);
    roller.scrollTimer = setTimeout(() => {
      const options = [...roller.querySelectorAll(".roller-option")];
      const rowHeight = options[0]?.offsetHeight || 42;
      const index = Math.max(0, Math.min(options.length - 1, Math.round(roller.scrollTop / rowHeight)));
      selectRollerValue(rollerId, options[index].dataset.value, true);
    }, 80);
  }, { passive: true });
  roller.addEventListener("keydown", (event) => {
    if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const options = [...roller.querySelectorAll(".roller-option")];
    const current = options.findIndex((option) => option.classList.contains("is-selected"));
    const offset = event.key === "ArrowUp" ? -1 : 1;
    const next = Math.max(0, Math.min(options.length - 1, current + offset));
    selectRollerValue(rollerId, options[next].dataset.value, true);
  });
  selectRollerValue(rollerId, el(inputId).value);
}

function setupTimerDurationPicker() {
  const padded = (value) => String(value).padStart(2, "0");
  setupRoller("timerMinuteRoller", "timerMinutes", timerMinuteValues());
  setupRoller("timerSecondRoller", "timerSeconds", Array.from({ length: 60 }, (_, value) => value), padded);
  setupRoller("scheduleHourRoller", "scheduleHour", Array.from({ length: 24 }, (_, value) => value), padded);
  setupRoller("scheduleMinuteRoller", "scheduleMinute", Array.from({ length: 60 }, (_, value) => value), padded);
  setupRoller("captureTimeoutRoller", "captureTimeoutMobile", Array.from({ length: 30 }, (_, index) => index + 1));
  el("scheduleHour").addEventListener("change", syncScheduleTimeFromRollers);
  el("scheduleMinute").addEventListener("change", syncScheduleTimeFromRollers);
  el("scheduleTime").addEventListener("change", () => setScheduleTimeFields(el("scheduleTime").value));
  el("captureTimeoutMobile").addEventListener("change", () => {
    el("captureTimeout").value = el("captureTimeoutMobile").value;
  });
  el("captureTimeout").addEventListener("change", () => {
    selectRollerValue("captureTimeoutRoller", el("captureTimeout").value);
  });
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

function resetNodeForm() {
  editingNodeId = null;
  el("nodeForm").reset();
  el("nodeDialogTitle").textContent = "Add node";
  el("saveNode").textContent = "Save node";
}

function openNodeDialog(node = null) {
  resetNodeForm();
  if (node) {
    editingNodeId = node.id;
    el("nodeDialogTitle").textContent = "Edit node";
    el("saveNode").textContent = "Save changes";
    el("nodeForm").elements.name.value = node.name;
    el("nodeForm").elements.base_url.value = node.base_url.replace(/^https?:\/\//, "");
  }
  el("nodeDialog").showModal();
}

function renderNodeConfiguration() {
  const target = el("nodeList");
  if (draggedNodeId !== null) return;
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
            <strong class="node-config-name">${escapeHtml(node.name)}</strong>
            <div class="node-meta">
              <span class="muted">${escapeHtml(node.base_url.replace(/^https?:\/\//, ""))} - ${signalCount} signal${signalCount === 1 ? "" : "s"}</span>
              ${nodeHealthView(node)}
            </div>
          </div>
          <div class="row-actions node-row-actions">
            <button class="secondary icon-button" type="button" data-edit-node="${node.id}" aria-label="Edit ${escapeHtml(node.name)}" title="Edit node">${actionIcon("pencil")}</button>
            <button class="danger icon-button" type="button" data-delete-node="${node.id}" aria-label="Delete ${escapeHtml(node.name)}" title="Delete node">${actionIcon("trash")}</button>
          </div>
        </div>
      `;
    })
    .join("");

  target.querySelectorAll("[data-edit-node]").forEach((button) => {
    button.addEventListener("click", () => {
      const node = state.nodes.find((item) => item.id === Number(button.dataset.editNode));
      if (node) openNodeDialog(node);
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

function configuredTimerLabel(seconds) {
  return `${Number((seconds / 60).toFixed(2))} min`;
}

function activeTimerLabel(runAtUtc) {
  const remaining = Math.max(0, Math.ceil((Date.parse(runAtUtc) - Date.now()) / 1000));
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function timerActionLabel(timer) {
  if (timer.target_kind === "workflow") return workflowLabel(timer.target_id);

  const button = state.buttons.find((item) => item.id === Number(timer.target_id));
  if (!button) return "Missing signal";
  const { node } = buttonContext(button);
  return node ? `${node.name} - ${button.name}` : button.name;
}

function updateTimerCountdowns() {
  document.querySelectorAll("[data-timer-countdown]").forEach((output) => {
    output.textContent = output.dataset.runAt
      ? activeTimerLabel(output.dataset.runAt)
      : configuredTimerLabel(Number(output.dataset.duration));
  });
}

function scheduleCountdownUpdate() {
  updateTimerCountdowns();
  const nextSecondDelay = 1000 - (Date.now() % 1000) + 10;
  setTimeout(scheduleCountdownUpdate, nextSecondDelay);
}

function setTimerDurationFields(seconds) {
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  selectRollerValue("timerMinuteRoller", minutes);
  selectRollerValue("timerSecondRoller", remainingSeconds);
  el("timerForm").elements.minutes.value = Number((seconds / 60).toFixed(4));
}

function openTimerDialog(timer = null) {
  const form = el("timerForm");
  form.reset();
  editingTimerId = timer?.id ?? null;
  el("timerDialogTitle").textContent = timer ? "Edit timer" : "Add timer";
  el("saveTimer").textContent = timer ? "Save changes" : "Save timer";
  if (timer) {
    form.elements.target.value = `${timer.target_kind}:${timer.target_id}`;
    setTimerDurationFields(timer.duration_seconds);
  } else {
    setTimerDurationFields(30 * 60);
  }
  el("timerDialog").showModal();
}

function renderTimerPresets() {
  const target = el("timerPresetList");
  if (!state.timer_presets.length) {
    target.innerHTML = '<div class="empty">No timers.</div>';
    return;
  }

  target.innerHTML = state.timer_presets
    .map((timer) => `
      <article class="timer-preset${timer.active ? " is-active" : ""}">
        <button
          class="timer-preset-trigger"
          type="button"
          data-toggle-timer="${timer.id}"
          aria-label="${timer.active ? "Cancel" : "Start"} timer for ${escapeHtml(timerActionLabel(timer))}"
        >
          <strong>${escapeHtml(timerActionLabel(timer))}</strong>
          <output
            data-timer-countdown="${timer.id}"
            data-duration="${timer.duration_seconds}"
            data-run-at="${timer.active ? escapeHtml(timer.run_at_utc) : ""}"
          >${timer.active ? activeTimerLabel(timer.run_at_utc) : configuredTimerLabel(timer.duration_seconds)}</output>
          ${timer.error ? `<span class="timer-preset-error">Last run failed</span>` : ""}
        </button>
        <div class="timer-preset-actions">
          <button class="timer-preset-edit icon-button" type="button" data-edit-timer="${timer.id}" aria-label="Edit timer for ${escapeHtml(timerActionLabel(timer))}" title="${timer.active ? "Cancel timer before editing" : "Edit timer"}" ${timer.active ? "disabled" : ""}>${actionIcon("pencil")}</button>
          <button class="timer-preset-delete icon-button" type="button" data-delete-timer="${timer.id}" aria-label="Delete timer for ${escapeHtml(timerActionLabel(timer))}" title="Delete timer">${actionIcon("trash")}</button>
        </div>
      </article>
    `)
    .join("");

  target.querySelectorAll("[data-toggle-timer]").forEach((button) => {
    button.addEventListener("click", async () => {
      const timer = state.timer_presets.find((item) => item.id === Number(button.dataset.toggleTimer));
      if (!timer) return;
      const wasActive = timer.active;
      button.disabled = true;
      try {
        const result = await api(`/api/timer-presets/${timer.id}/${wasActive ? "cancel" : "start"}`, { method: "POST" });
        timer.active = !wasActive;
        timer.run_at_utc = wasActive ? null : result.run_at_utc;
        timer.error = null;
        renderTimerPresets();
        updateTimerCountdowns();
        showToast(wasActive ? "Timer cancelled" : "Timer started");
      } catch (error) {
        showToast(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  });

  target.querySelectorAll("[data-edit-timer]").forEach((button) => {
    button.addEventListener("click", () => {
      const timer = state.timer_presets.find((item) => item.id === Number(button.dataset.editTimer));
      if (timer && !timer.active) openTimerDialog(timer);
    });
  });

  target.querySelectorAll("[data-delete-timer]").forEach((button) => {
    button.addEventListener("click", async () => {
      const timer = state.timer_presets.find((item) => item.id === Number(button.dataset.deleteTimer));
      if (!timer || !window.confirm(`Delete timer for "${timerActionLabel(timer)}"?`)) return;
      try {
        await api(`/api/timer-presets/${timer.id}`, { method: "DELETE" });
        showToast("Timer deleted");
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });
}

function scheduleItems() {
  return [
    ...state.schedules.map((schedule) => ({ ...schedule, kind: "signal", targetLabel: buttonLabel(schedule.button_id) })),
    ...state.workflow_schedules.map((schedule) => ({ ...schedule, kind: "workflow", targetLabel: workflowLabel(schedule.workflow_id) })),
  ].sort((a, b) => a.time_of_day.localeCompare(b.time_of_day) || a.name.localeCompare(b.name));
}

function scheduleTargetValue(schedule) {
  return schedule.kind === "workflow"
    ? `workflow:${schedule.workflow_id}`
    : `signal:${schedule.button_id}`;
}

function syncScheduleTimeFromRollers() {
  const hour = String(el("scheduleHour").value).padStart(2, "0");
  const minute = String(el("scheduleMinute").value).padStart(2, "0");
  el("scheduleTime").value = `${hour}:${minute}`;
}

function setScheduleTimeFields(time) {
  const [hour, minute] = String(time || "00:00").split(":").map(Number);
  el("scheduleTime").value = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
  selectRollerValue("scheduleHourRoller", hour);
  selectRollerValue("scheduleMinuteRoller", minute);
}

function openScheduleDialog(schedule = null) {
  const form = el("scheduleForm");
  form.reset();
  editingSchedule = schedule ? { id: schedule.id, kind: schedule.kind } : null;
  el("scheduleDialogTitle").textContent = schedule ? "Edit schedule" : "Add schedule";
  el("saveSchedule").textContent = schedule ? "Save changes" : "Save schedule";
  setScheduleTimeFields(schedule?.time_of_day || "00:00");
  if (schedule) {
    form.elements.target.value = scheduleTargetValue(schedule);
    form.elements.name.value = schedule.name;
    form.elements.days.forEach((input) => {
      input.checked = schedule.days.includes(Number(input.value));
    });
    form.elements.enabled.checked = schedule.enabled;
  }
  el("scheduleDialog").showModal();
}

function renderSchedules() {
  const target = el("scheduleList");
  const schedules = scheduleItems();

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
            ${escapeHtml(daysLabel(schedule.days))}
          </div>
        </div>
        <div class="row-actions schedule-row-actions">
          <label class="schedule-list-switch" title="${schedule.enabled ? "Disable" : "Enable"} ${escapeHtml(schedule.name)}">
            <input
              class="switch-input"
              type="checkbox"
              role="switch"
              data-schedule-enabled="${schedule.id}"
              data-schedule-kind="${schedule.kind}"
              aria-label="Enable ${escapeHtml(schedule.name)}"
              ${schedule.enabled ? "checked" : ""}
            />
            <span class="switch-track" aria-hidden="true"></span>
          </label>
          <button class="secondary icon-button" type="button" data-edit-schedule="${schedule.id}" data-schedule-kind="${schedule.kind}" aria-label="Edit ${escapeHtml(schedule.name)}" title="Edit schedule">${actionIcon("pencil")}</button>
        </div>
      </div>
    `)
    .join("");

  target.querySelectorAll("[data-schedule-enabled]").forEach((input) => {
    input.addEventListener("change", async () => {
      const enabled = input.checked;
      input.disabled = true;
      try {
        await api(`/api/schedule-items/${input.dataset.scheduleKind}/${input.dataset.scheduleEnabled}/enabled`, {
          method: "PUT",
          body: JSON.stringify({ enabled }),
        });
        showToast(enabled ? "Schedule enabled" : "Schedule disabled");
        await loadState();
      } catch (error) {
        input.checked = !enabled;
        showToast(error.message, true);
      } finally {
        input.disabled = false;
      }
    });
  });

  target.querySelectorAll("[data-edit-schedule]").forEach((button) => {
    button.addEventListener("click", () => {
      const schedule = schedules.find(
        (item) => item.id === Number(button.dataset.editSchedule) && item.kind === button.dataset.scheduleKind,
      );
      if (schedule) openScheduleDialog(schedule);
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

function addWorkflowStepRow(delaySeconds = 30 * 60, buttonId = "") {
  workflowStepKey += 1;
  const key = workflowStepKey;
  const minutes = Math.floor(delaySeconds / 60);
  const seconds = delaySeconds % 60;
  const decimalMinutes = Number((delaySeconds / 60).toFixed(4));
  const row = document.createElement("div");
  row.className = "workflow-step";
  row.innerHTML = `
    <input class="workflow-delay-desktop" name="delay_minutes" aria-label="Delay in min" type="number" min="0" max="10080" step="any" value="${decimalMinutes}" required />
    <div class="workflow-delay-mobile" role="group" aria-label="Delay">
      <div class="roller-field compact-roller-field">
        <span>Min</span>
        <div class="roller-frame"><div id="workflowMinuteRoller${key}" class="roller" role="listbox" tabindex="0" aria-label="Delay min"></div></div>
        <input id="workflowMinutes${key}" type="hidden" value="${minutes}" />
      </div>
      <div class="roller-field compact-roller-field">
        <span>Sec</span>
        <div class="roller-frame"><div id="workflowSecondRoller${key}" class="roller" role="listbox" tabindex="0" aria-label="Delay sec"></div></div>
        <input id="workflowSeconds${key}" type="hidden" value="${seconds}" />
      </div>
    </div>
    <select name="button_id" aria-label="Signal" required></select>
    <button class="danger icon-button" type="button" data-remove-step aria-label="Remove workflow step" title="Remove step">${actionIcon("trash")}</button>
  `;
  el("workflowSteps").appendChild(row);
  const padded = (value) => String(value).padStart(2, "0");
  setupRoller(`workflowMinuteRoller${key}`, `workflowMinutes${key}`, timerMinuteValues());
  setupRoller(`workflowSecondRoller${key}`, `workflowSeconds${key}`, Array.from({ length: 60 }, (_, value) => value), padded);
  syncWorkflowStepOptions();
  if (buttonId) row.querySelector("select[name='button_id']").value = String(buttonId);
  row.querySelector("[data-remove-step]").addEventListener("click", () => {
    if (document.querySelectorAll(".workflow-step").length > 1) row.remove();
  });
}

function resetWorkflowForm() {
  editingWorkflowId = null;
  el("workflowForm").reset();
  el("workflowSteps").innerHTML = "";
  el("workflowDialogTitle").textContent = "Add workflow";
  el("saveWorkflowButton").textContent = "Save workflow";
}

function editWorkflow(workflowId) {
  const workflow = state.workflows.find((item) => item.id === Number(workflowId));
  if (!workflow) return;

  resetWorkflowForm();
  editingWorkflowId = workflow.id;
  el("workflowDialogTitle").textContent = "Edit workflow";
  el("workflowForm").querySelector("input[name='name']").value = workflow.name;
  const steps = workflowSteps(workflow.id);
  steps.forEach((step) => {
    addWorkflowStepRow(step.delay_seconds, step.button_id);
  });
  if (!steps.length) addWorkflowStepRow(0);

  el("saveWorkflowButton").textContent = "Save changes";
  el("workflowDialog").showModal();
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
  const pagination = el("eventPagination");
  if (!state.events.length) {
    target.innerHTML = '<div class="empty">No events.</div>';
    pagination.innerHTML = "";
    return;
  }
  const dateFormatter = new Intl.DateTimeFormat("en-MY", {
    timeZone: state.timezone || "Asia/Kuala_Lumpur",
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  const timeFormatter = new Intl.DateTimeFormat("en-MY", {
    timeZone: state.timezone || "Asia/Kuala_Lumpur",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
  target.innerHTML = `
    <table class="event-table">
      <thead>
        <tr><th scope="col">Log</th><th scope="col">Date &amp; time (MYT)</th></tr>
      </thead>
      <tbody>
        ${state.events.map((event) => {
          const createdAt = new Date(event.created_at);
          const failed = event.status === "failed";
          return `
            <tr class="event-row ${failed ? "event-row-failed" : "event-row-success"}">
              <td>${escapeHtml(event.message)}</td>
              <td class="event-time-cell">
                <time datetime="${escapeHtml(event.created_at)}">
                  <span>${escapeHtml(dateFormatter.format(createdAt))}</span>
                  <span>${escapeHtml(timeFormatter.format(createdAt))}</span>
                </time>
              </td>
            </tr>
          `;
        }).join("")}
      </tbody>
    </table>
  `;
  pagination.innerHTML = eventTotalPages > 1 ? `
    <button class="secondary icon-button pagination-icon-button" type="button" data-event-page="${eventPage - 1}" aria-label="Previous page" title="Previous page" ${eventPage === 1 ? "disabled" : ""}>&lt;</button>
    <span>Page ${eventPage} of ${eventTotalPages}</span>
    <button class="secondary icon-button pagination-icon-button" type="button" data-event-page="${eventPage + 1}" aria-label="Next page" title="Next page" ${eventPage === eventTotalPages ? "disabled" : ""}>&gt;</button>
  ` : "";
  pagination.querySelectorAll("[data-event-page]").forEach((button) => {
    button.addEventListener("click", () => loadEvents(Number(button.dataset.eventPage)));
  });
}

async function loadEvents(page = eventPage) {
  const result = await api(`/api/events?page=${page}&page_size=${eventPageSize}`);
  state.events = result.items;
  eventPage = result.page;
  eventTotalPages = result.total_pages;
  renderEvents();
}

async function loadState() {
  try {
    const nextState = await api("/api/state");
    Object.assign(state, nextState);
    el("apiStatus").textContent = `${state.nodes.length} nodes - ${actions().length + state.ac_controllers.length} actions`;
    el("clock").textContent = state.timezone;
    renderControllerTabs();
    updateSelectors();
    renderActions();
    renderTimerPresets();
    renderSchedules();
    renderNodeConfiguration();
    renderSignalConfiguration();
    renderWorkflows();
    await loadEvents(eventPage);
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
  activeAcControllerNodeId = null;
});
el("acNode").addEventListener("change", (event) => {
  activeAcControllerNodeId = Number(event.currentTarget.value) || null;
  renderAcController();
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

el("addTimer").addEventListener("click", () => openTimerDialog());
el("closeTimerDialog").addEventListener("click", () => el("timerDialog").close());
el("cancelTimerDialog").addEventListener("click", () => el("timerDialog").close());
el("timerDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
el("timerDialog").addEventListener("close", () => {
  editingTimerId = null;
});

el("addNode").addEventListener("click", () => openNodeDialog());
el("closeNodeDialog").addEventListener("click", () => el("nodeDialog").close());
el("cancelNode").addEventListener("click", () => el("nodeDialog").close());
el("nodeDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
el("nodeDialog").addEventListener("close", resetNodeForm);

el("addWorkflow").addEventListener("click", () => {
  resetWorkflowForm();
  addWorkflowStepRow();
  el("workflowDialog").showModal();
});
el("closeWorkflowDialog").addEventListener("click", () => el("workflowDialog").close());
el("cancelWorkflowEdit").addEventListener("click", () => el("workflowDialog").close());
el("workflowDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
el("workflowDialog").addEventListener("close", resetWorkflowForm);

el("addSchedule").addEventListener("click", () => openScheduleDialog());
el("closeScheduleDialog").addEventListener("click", () => el("scheduleDialog").close());
el("cancelSchedule").addEventListener("click", () => el("scheduleDialog").close());
el("scheduleDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
el("scheduleDialog").addEventListener("close", () => {
  editingSchedule = null;
});

el("refreshButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.classList.add("is-loading");
  button.setAttribute("aria-label", "Checking node status");
  try {
    const result = await api("/api/node-health/refresh", { method: "POST" });
    await loadState();
    showToast(`${result.online} of ${result.total} nodes online`);
  } catch (error) {
    showToast(`Health check failed: ${error.message}`, true);
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.setAttribute("aria-label", "Refresh node status");
  }
});
el("addWorkflowStep").addEventListener("click", () => addWorkflowStepRow());

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
  const saveButton = el("saveNode");
  try {
    const wasEditing = editingNodeId !== null;
    saveButton.disabled = true;
    await api(wasEditing ? `/api/nodes/${editingNodeId}` : "/api/nodes", {
      method: wasEditing ? "PUT" : "POST",
      body: JSON.stringify(formJson(form)),
    });
    el("nodeDialog").close();
    showToast(wasEditing ? "Node updated" : "Node saved");
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    saveButton.disabled = false;
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
    showToast(`Capturing ${signalLabel} signal - ${secondsRemaining > 0 ? `${secondsRemaining} sec remaining` : "finishing"}`, false, 0);
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
  const saveButton = el("saveTimer");
  try {
    const target = parseTarget(data.get("target"));
    const useMobilePicker = window.matchMedia("(max-width: 620px)").matches;
    const seconds = useMobilePicker
      ? delayToSeconds(
          Number(data.get("mobile_minutes")) * 60 + Number(data.get("mobile_seconds")),
          1,
        )
      : delayToSeconds(data.get("minutes"), 60);
    saveButton.disabled = true;
    const wasEditing = editingTimerId !== null;
    await api(wasEditing ? `/api/timer-presets/${editingTimerId}` : "/api/timer-presets", {
      method: wasEditing ? "PUT" : "POST",
      body: JSON.stringify({ target_kind: target.kind, target_id: target.id, seconds }),
    });
    el("timerDialog").close();
    showToast(wasEditing ? "Timer updated" : "Timer saved");
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    saveButton.disabled = false;
  }
});

el("workflowForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const rows = [...form.querySelectorAll(".workflow-step")];
  const saveButton = el("saveWorkflowButton");
  try {
    const useMobilePicker = window.matchMedia("(max-width: 620px)").matches;
    const steps = rows.map((row) => ({
      button_id: Number(row.querySelector("select[name='button_id']").value),
      delay_seconds: useMobilePicker
        ? delayToSeconds(
            Number(row.querySelector(".workflow-delay-mobile input[id^='workflowMinutes']").value) * 60
              + Number(row.querySelector(".workflow-delay-mobile input[id^='workflowSeconds']").value),
            1,
            true,
          )
        : delayToSeconds(row.querySelector("input[name='delay_minutes']").value, 60, true),
    }));
    if (!steps.length || steps.some((step) => !step.button_id)) {
      throw new Error("Add at least one workflow step with a signal");
    }
    const data = new FormData(form);
    const wasEditing = editingWorkflowId !== null;
    saveButton.disabled = true;
    await api(editingWorkflowId ? `/api/workflows/${editingWorkflowId}` : "/api/workflows", {
      method: editingWorkflowId ? "PUT" : "POST",
      body: JSON.stringify({ name: data.get("name") || "Workflow", steps }),
    });
    el("workflowDialog").close();
    showToast(wasEditing ? "Workflow updated" : "Workflow saved");
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    saveButton.disabled = false;
  }
});

el("scheduleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const saveButton = el("saveSchedule");
  try {
    const target = parseTarget(data.get("target"));
    const selected = actions().find((action) => action.value === data.get("target"));
    const payload = {
      target_kind: target.kind,
      target_id: target.id,
      name: data.get("name") || `${selected?.name || "Action"} schedule`,
      time_of_day: data.get("time_of_day"),
      days: data.getAll("days").map(Number),
      enabled: data.has("enabled"),
    };
    if (!payload.days.length) throw new Error("Choose at least one day");

    saveButton.disabled = true;
    const path = editingSchedule
      ? `/api/schedule-items/${editingSchedule.kind}/${editingSchedule.id}`
      : "/api/schedule-items";
    const wasEditing = editingSchedule !== null;
    await api(path, {
      method: wasEditing ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    el("scheduleDialog").close();
    showToast(wasEditing ? "Schedule updated" : "Schedule saved");
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    saveButton.disabled = false;
  }
});

activateTab(location.hash.slice(1), false);
setupTimerDurationPicker();
loadState();
scheduleCountdownUpdate();
setInterval(loadState, 5000);
