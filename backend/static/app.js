const state = {
  nodes: [],
  devices: [],
  buttons: [],
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

const el = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, isError = false) {
  const toast = el("toast");
  toast.textContent = message;
  toast.hidden = false;
  toast.style.background = isError ? "#7a1e1e" : "#17201b";
  clearTimeout(showToast.timeout);
  showToast.timeout = setTimeout(() => {
    toast.hidden = true;
  }, 3200);
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

function optionList(items, getLabel) {
  if (!items.length) return '<option value="">None</option>';
  return items.map((item) => `<option value="${item.id}">${escapeHtml(getLabel(item))}</option>`).join("");
}

function updateSelectors() {
  const nodeOptions = optionList(state.nodes, (node) => `${node.room || "No room"} / ${node.name}`);
  const buttonOptions = optionList(state.buttons, (button) => {
    return buttonLabel(button.id);
  });
  const workflowOptions = optionList(state.workflows, (workflow) => workflow.name);

  el("captureNode").innerHTML = nodeOptions;
  el("timerButton").innerHTML = buttonOptions;
  el("scheduleButton").innerHTML = buttonOptions;
  el("workflowScheduleWorkflow").innerHTML = workflowOptions;
  syncWorkflowStepOptions();
}

function renderDevices() {
  const grid = el("deviceGrid");
  if (!state.nodes.length) {
    grid.innerHTML = '<div class="empty">Add an ESP32 node, then learn a signal.</div>';
    return;
  }

  grid.innerHTML = state.nodes
    .map((node) => {
      const nodeDevices = state.devices.filter((device) => device.node_id === node.id);
      const deviceIds = new Set(nodeDevices.map((device) => device.id));
      const buttons = state.buttons.filter((button) => deviceIds.has(button.device_id));
      const buttonHtml = buttons.length
        ? buttons
            .map((button) => {
              const count = button.stats?.press_count || 0;
              return `
                <button class="button-tile" type="button" data-press="${button.id}">
                  ${escapeHtml(button.name)}
                  <span>${escapeHtml(button.signal_type.toUpperCase())} - ${count} sent</span>
                </button>
              `;
            })
            .join("")
        : '<div class="empty">No signals learned.</div>';
      return `
        <article class="device">
          <div class="device-header">
            <div>
              <h3>${escapeHtml(node.name)}</h3>
              <div class="muted">${escapeHtml(node.room || "No room")} - ${escapeHtml(node.base_url)}</div>
            </div>
            <span class="pill">${node.enabled ? "enabled" : "disabled"}</span>
          </div>
          <div class="device-buttons">${buttonHtml}</div>
        </article>
      `;
    })
    .join("");

  document.querySelectorAll("[data-press]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await api(`/api/buttons/${button.dataset.press}/press`, { method: "POST" });
        showToast(result.message);
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  });
}

function renderTimers() {
  const target = el("timerList");
  if (!state.timers.length) {
    target.innerHTML = '<div class="empty">No timers.</div>';
    return;
  }
  target.innerHTML = state.timers
    .map((timer) => `
      <div class="row">
        <div>
          <strong>${escapeHtml(timer.name)}</strong>
          <div class="muted">${escapeHtml(timer.status)} - ${escapeHtml(timer.run_at_utc)}</div>
          ${timer.error ? `<div class="bad">${escapeHtml(timer.error)}</div>` : ""}
        </div>
        ${timer.status === "pending" ? `<button class="secondary" type="button" data-cancel="${timer.id}">Cancel</button>` : ""}
      </div>
    `)
    .join("");

  document.querySelectorAll("[data-cancel]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/timers/${button.dataset.cancel}/cancel`, { method: "POST" });
      await loadState();
    });
  });
}

function daysLabel(days) {
  if (days.length === 7) return "daily";
  return days.map((day) => dayNames[day]).join(", ");
}

function nextWorkflowStep(runId) {
  return state.workflow_run_steps
    .filter((step) => step.run_id === runId && ["waiting", "pending", "running"].includes(step.status))
    .sort((a, b) => a.step_order - b.step_order)[0];
}

function renderActiveJobs() {
  const target = el("activeJobList");
  const jobs = [];

  state.timers
    .filter((timer) => ["pending", "running"].includes(timer.status))
    .forEach((timer) => {
      jobs.push({
        type: "timer",
        name: timer.name,
        detail: `${buttonLabel(timer.button_id)} - ${timer.status} - ${timer.run_at_utc}`,
      });
    });

  state.schedules
    .filter((schedule) => schedule.enabled)
    .forEach((schedule) => {
      jobs.push({
        type: "button schedule",
        name: schedule.name,
        detail: `${buttonLabel(schedule.button_id)} - ${schedule.time_of_day} - ${daysLabel(schedule.days)}`,
      });
    });

  state.workflow_schedules
    .filter((schedule) => schedule.enabled)
    .forEach((schedule) => {
      jobs.push({
        type: "workflow schedule",
        name: schedule.name,
        detail: `${workflowLabel(schedule.workflow_id)} - ${schedule.time_of_day} - ${daysLabel(schedule.days)}`,
      });
    });

  state.workflow_runs
    .filter((run) => ["pending", "running"].includes(run.status))
    .forEach((run) => {
      const nextStep = nextWorkflowStep(run.id);
      const next = nextStep ? `${formatDelay(nextStep.delay_seconds)} -> ${buttonLabel(nextStep.button_id)}` : "no next step";
      jobs.push({
        type: "workflow run",
        name: run.name,
        detail: `${run.status} - next ${next}`,
      });
    });

  if (!jobs.length) {
    target.innerHTML = '<div class="empty">No active jobs.</div>';
    return;
  }

  target.innerHTML = jobs
    .map((job) => `
      <div class="row">
        <div>
          <strong>${escapeHtml(job.name)}</strong>
          <div class="muted">${escapeHtml(job.type)}</div>
          <span>${escapeHtml(job.detail)}</span>
        </div>
      </div>
    `)
    .join("");
}

function renderSchedules() {
  const target = el("scheduleList");
  if (!state.schedules.length) {
    target.innerHTML = '<div class="empty">No schedules.</div>';
    return;
  }
  target.innerHTML = state.schedules
    .map((schedule) => `
      <div class="row">
        <div>
          <strong>${escapeHtml(schedule.name)}</strong>
          <div class="muted">
            ${escapeHtml(schedule.time_of_day)} - ${schedule.days.map((day) => dayNames[day]).join(", ")}
            - ${schedule.enabled ? "enabled" : "paused"}
          </div>
        </div>
        <button class="secondary" type="button" data-toggle="${schedule.id}">
          ${schedule.enabled ? "Pause" : "Enable"}
        </button>
      </div>
    `)
    .join("");

  document.querySelectorAll("[data-toggle]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/schedules/${button.dataset.toggle}/toggle`, { method: "POST" });
      await loadState();
    });
  });
}

function formatDelay(seconds) {
  if (seconds === 0) return "immediate";
  if (seconds % 3600 === 0) {
    const hours = seconds / 3600;
    return `${hours} hr${hours === 1 ? "" : "s"}`;
  }
  if (seconds % 60 === 0) {
    const minutes = seconds / 60;
    return `${minutes} min`;
  }
  return `${seconds} sec`;
}

function buttonLabel(buttonId) {
  const button = state.buttons.find((item) => item.id === Number(buttonId));
  if (!button) return "Missing button";
  const device = state.devices.find((item) => item.id === button.device_id);
  const node = state.nodes.find((item) => item.id === device?.node_id);
  return `${node?.name || "Node"} / ${button.name}`;
}

function workflowLabel(workflowId) {
  const workflow = state.workflows.find((item) => item.id === Number(workflowId));
  return workflow?.name || "Missing workflow";
}

function syncWorkflowStepOptions() {
  const options = optionList(state.buttons, (button) => buttonLabel(button.id));
  document.querySelectorAll("#workflowSteps select[name='button_id']").forEach((select) => {
    const selected = select.value;
    select.innerHTML = options;
    if (selected) select.value = selected;
  });
}

function addWorkflowStepRow(delay = 30, unit = 60, buttonId = "") {
  const row = document.createElement("div");
  row.className = "workflow-step";
  row.innerHTML = `
    <input name="delay" type="number" min="0" max="10080" value="${delay}" required />
    <select name="unit">
      <option value="60"${unit === 60 ? " selected" : ""}>minutes</option>
      <option value="3600"${unit === 3600 ? " selected" : ""}>hours</option>
    </select>
    <select name="button_id" required></select>
    <button class="secondary" type="button" data-remove-step>Remove</button>
  `;
  el("workflowSteps").appendChild(row);
  syncWorkflowStepOptions();
  if (buttonId) row.querySelector("select[name='button_id']").value = String(buttonId);
  row.querySelector("[data-remove-step]").addEventListener("click", () => {
    if (document.querySelectorAll(".workflow-step").length > 1) row.remove();
  });
}

function splitDelay(seconds) {
  if (seconds > 0 && seconds % 3600 === 0) {
    return { delay: seconds / 3600, unit: 3600 };
  }
  return { delay: seconds / 60, unit: 60 };
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

  const steps = state.workflow_steps
    .filter((step) => step.workflow_id === workflow.id)
    .sort((a, b) => a.step_order - b.step_order);

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
  const steps = state.workflow_steps.filter((step) => step.workflow_id === workflowId);
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

function workflowRunSummary(runId) {
  const steps = state.workflow_run_steps.filter((step) => step.run_id === runId);
  if (!steps.length) return "";
  return steps
    .map((step) => `${step.step_order}:${step.status}`)
    .join(" / ");
}

function renderWorkflows() {
  const target = el("workflowList");
  if (!state.workflows.length && !state.workflow_runs.length) {
    target.innerHTML = '<div class="empty">No workflows.</div>';
    return;
  }

  const workflowsHtml = state.workflows
    .map((workflow) => `
      <div class="row">
        <div>
          <strong>${escapeHtml(workflow.name)}</strong>
          ${workflowStepSummary(workflow.id)}
        </div>
        <div class="row-actions">
          <button class="secondary" type="button" data-edit-workflow="${workflow.id}">Edit</button>
          <button type="button" data-run-workflow="${workflow.id}">Run</button>
        </div>
      </div>
    `)
    .join("");

  const runsHtml = state.workflow_runs
    .map((run) => `
      <div class="row">
        <div>
          <strong>Run: ${escapeHtml(run.name)}</strong>
          <div class="muted">${escapeHtml(run.status)} - ${escapeHtml(run.created_at)}</div>
          <span>${escapeHtml(workflowRunSummary(run.id))}</span>
          ${run.error ? `<div class="bad">${escapeHtml(run.error)}</div>` : ""}
        </div>
        ${["pending", "running"].includes(run.status) ? `<button class="secondary" type="button" data-cancel-run="${run.id}">Cancel</button>` : ""}
      </div>
    `)
    .join("");

  target.innerHTML = workflowsHtml + runsHtml;

  document.querySelectorAll("[data-edit-workflow]").forEach((button) => {
    button.addEventListener("click", () => editWorkflow(button.dataset.editWorkflow));
  });

  document.querySelectorAll("[data-run-workflow]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api(`/api/workflows/${button.dataset.runWorkflow}/run`, { method: "POST" });
        showToast("Workflow started");
        await loadState();
      } catch (error) {
        showToast(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-cancel-run]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/workflow-runs/${button.dataset.cancelRun}/cancel`, { method: "POST" });
      await loadState();
    });
  });
}

function renderWorkflowSchedules() {
  const target = el("workflowScheduleList");
  if (!state.workflow_schedules.length) {
    target.innerHTML = '<div class="empty">No workflow schedules.</div>';
    return;
  }
  target.innerHTML = state.workflow_schedules
    .map((schedule) => `
      <div class="row">
        <div>
          <strong>${escapeHtml(schedule.name)}</strong>
          <div class="muted">
            ${escapeHtml(workflowLabel(schedule.workflow_id))} - ${escapeHtml(schedule.time_of_day)}
            - ${schedule.days.map((day) => dayNames[day]).join(", ")}
            - ${schedule.enabled ? "enabled" : "paused"}
          </div>
        </div>
        <button class="secondary" type="button" data-toggle-workflow-schedule="${schedule.id}">
          ${schedule.enabled ? "Pause" : "Enable"}
        </button>
      </div>
    `)
    .join("");

  document.querySelectorAll("[data-toggle-workflow-schedule]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/workflow-schedules/${button.dataset.toggleWorkflowSchedule}/toggle`, { method: "POST" });
      await loadState();
    });
  });
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
    el("apiStatus").textContent = `${state.nodes.length} nodes - ${state.buttons.length} buttons`;
    el("clock").textContent = state.timezone;
    updateSelectors();
    renderDevices();
    renderTimers();
    renderActiveJobs();
    renderWorkflows();
    renderWorkflowSchedules();
    renderSchedules();
    renderEvents();
  } catch (error) {
    el("apiStatus").textContent = "Offline";
    showToast(error.message, true);
  }
}

function formJson(form) {
  return Object.fromEntries(new FormData(form).entries());
}

el("refreshButton").addEventListener("click", loadState);
el("addWorkflowStep").addEventListener("click", () => addWorkflowStepRow());
el("cancelWorkflowEdit").addEventListener("click", resetWorkflowForm);

el("nodeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/nodes", { method: "POST", body: JSON.stringify(formJson(form)) });
    form.reset();
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

el("captureForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = formJson(form);
  const timeoutMs = Number(body.timeout || 8) * 1000;
  try {
    if (!Number(body.node_id)) throw new Error("Add a node first");
    showToast("Listening for signal");
    const result = await api("/api/signals/learn", {
      method: "POST",
      body: JSON.stringify({
        node_id: Number(body.node_id),
        name: body.name,
        signal_type: body.signal_type,
        timeout_ms: timeoutMs,
      }),
    });
    showToast(`Saved ${result.name}`);
    form.reset();
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

el("timerForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = formJson(form);
  body.button_id = Number(body.button_id);
  body.seconds = Number(body.seconds);
  body.name = body.name || "Timer";
  try {
    await api("/api/timers", { method: "POST", body: JSON.stringify(body) });
    form.reset();
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

el("workflowForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const rows = [...document.querySelectorAll(".workflow-step")];
  const steps = rows.map((row) => {
    const delay = Number(row.querySelector("input[name='delay']").value);
    const unit = Number(row.querySelector("select[name='unit']").value);
    const buttonId = Number(row.querySelector("select[name='button_id']").value);
    return { button_id: buttonId, delay_seconds: delay * unit };
  });
  try {
    if (!steps.length || steps.some((step) => !step.button_id)) {
      throw new Error("Add at least one workflow step with a button");
    }
    const data = new FormData(form);
    await api(editingWorkflowId ? `/api/workflows/${editingWorkflowId}` : "/api/workflows", {
      method: editingWorkflowId ? "PUT" : "POST",
      body: JSON.stringify({ name: data.get("name") || "Workflow", steps }),
    });
    resetWorkflowForm();
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

el("scheduleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const days = data.getAll("days").map(Number);
  const body = {
    button_id: Number(data.get("button_id")),
    name: data.get("name") || "Schedule",
    time_of_day: data.get("time_of_day"),
    days,
    enabled: true,
  };
  try {
    await api("/api/schedules", { method: "POST", body: JSON.stringify(body) });
    form.reset();
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

el("workflowScheduleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const days = data.getAll("days").map(Number);
  const body = {
    workflow_id: Number(data.get("workflow_id")),
    name: data.get("name") || "Workflow schedule",
    time_of_day: data.get("time_of_day"),
    days,
    enabled: true,
  };
  try {
    if (!body.workflow_id) throw new Error("Create a workflow first");
    await api("/api/workflow-schedules", { method: "POST", body: JSON.stringify(body) });
    form.reset();
    await loadState();
  } catch (error) {
    showToast(error.message, true);
  }
});

addWorkflowStepRow();
loadState();
setInterval(loadState, 5000);
