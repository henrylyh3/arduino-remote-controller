let controllerSlug = decodeURIComponent(window.location.pathname.slice(1));
let controllerId = null;
let controller = null;
let controllers = [];
let timezone = "Asia/Kuala_Lumpur";

const el = (id) => document.getElementById(id);

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

function showToast(message, isError = false) {
  const toast = el("controllerToast");
  toast.textContent = message;
  toast.hidden = false;
  toast.style.background = isError ? "#7a1e1e" : "#17201b";
  toast.setAttribute("role", isError ? "alert" : "status");
  clearTimeout(showToast.timeout);
  showToast.timeout = setTimeout(() => {
    toast.hidden = true;
  }, 3200);
}

function fanLabel(fan) {
  return fan === "auto" ? "Auto" : fan;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderControllerTabs() {
  el("controllerTabs").innerHTML = controllers
    .map((item) => `
      <a class="tab${item.id === controllerId ? " is-active" : ""}" href="/${item.slug}" role="tab" aria-selected="${item.id === controllerId}">${escapeHtml(item.name)}</a>
    `)
    .join("");
}

function renderController() {
  if (!controller) return;
  document.title = `${controller.name} - ${controller.node_name}`;
  el("controllerTitle").textContent = controller.name;
  el("controllerNodeName").textContent = controller.node_name;
  el("controllerTemperatureDisplay").innerHTML = `${controller.temperature}&deg;`;
  el("controllerTemperatureValue").innerHTML = `${controller.temperature}&deg;C`;
  el("controllerDisplaySummary").textContent = `Fan ${fanLabel(controller.fan)} · Swing ${controller.swing ? "on" : "off"}`;
  el("controllerTemperatureDown").disabled = controller.temperature <= 16;
  el("controllerTemperatureUp").disabled = controller.temperature >= 30;
  el("controllerSwing").checked = controller.swing;
  document.querySelectorAll("[data-controller-fan]").forEach((button) => {
    const selected = button.dataset.controllerFan === controller.fan;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  const status = controller.health?.status || "unknown";
  const statusDot = el("controllerNodeStatus");
  statusDot.parentElement.className = `eyebrow controller-tab-node node-health-${status}`;
  statusDot.parentElement.setAttribute("aria-label", `Node ${status}`);
  el("apiStatus").textContent = `${controllers.length} controller${controllers.length === 1 ? "" : "s"}`;
  el("clock").textContent = timezone;
  renderControllerTabs();
  el("controllerLastSent").textContent = controller.last_sent_at
    ? `Last sent: ${controller.last_command} · ${new Date(controller.last_sent_at).toLocaleString("en-MY", { timeZone: timezone })}`
    : "No command sent yet";
}

function setBusy(busy) {
  el("controllerPanel").setAttribute("aria-busy", String(busy));
  el("controllerPanel").querySelectorAll("button, input").forEach((control) => {
    control.disabled = busy;
  });
  if (!busy) renderController();
}

async function sendController(changes = {}, powerToggle = false) {
  if (!controller) return;
  const command = {
    temperature: controller.temperature,
    fan: controller.fan,
    swing: controller.swing,
    ...changes,
    power_toggle: powerToggle,
  };
  setBusy(true);
  try {
    const result = await api(`/api/ac-controllers/${controller.id}/send`, {
      method: "POST",
      body: JSON.stringify(command),
    });
    controller = { ...controller, ...result.controller };
    renderController();
    showToast(result.message);
  } catch (error) {
    renderController();
    showToast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function loadController() {
  try {
    const result = await api(`/api/controller-pages/${encodeURIComponent(controllerSlug)}`);
    controller = result.controller;
    controllerId = controller.id;
    controllers = result.controllers;
    timezone = result.timezone;
    el("controllerMessage").hidden = true;
    el("controllerPanel").hidden = false;
    renderController();
  } catch (error) {
    el("controllerPanel").hidden = true;
    el("controllerMessage").textContent = error.message;
    el("controllerMessage").hidden = false;
  }
}

function openControllerNameDialog() {
  el("controllerName").value = controller.name;
  el("controllerNameDialog").showModal();
  el("controllerName").focus();
  el("controllerName").select();
}

el("controllerTemperatureDown").addEventListener("click", () => {
  sendController({ temperature: Math.max(16, controller.temperature - 1) });
});
el("controllerTemperatureUp").addEventListener("click", () => {
  sendController({ temperature: Math.min(30, controller.temperature + 1) });
});
document.querySelectorAll("[data-controller-fan]").forEach((button) => {
  button.addEventListener("click", () => sendController({ fan: button.dataset.controllerFan }));
});
el("controllerSwing").addEventListener("change", (event) => {
  sendController({ swing: event.currentTarget.checked });
});
el("controllerPower").addEventListener("click", () => sendController({}, true));
el("renameController").addEventListener("click", openControllerNameDialog);
el("closeControllerNameDialog").addEventListener("click", () => el("controllerNameDialog").close());
el("cancelControllerName").addEventListener("click", () => el("controllerNameDialog").close());
el("controllerNameDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
el("controllerNameForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const saveButton = el("saveControllerName");
  const name = el("controllerName").value.trim();
  if (!name) return;
  saveButton.disabled = true;
  try {
    const result = await api(`/api/ac-controllers/${controllerId}`, {
      method: "PUT",
      body: JSON.stringify({ name }),
    });
    controller.name = result.name;
    controllers = result.controllers;
    controllerSlug = result.slug;
    history.replaceState(null, "", `/${controllerSlug}`);
    el("controllerNameDialog").close();
    renderController();
    showToast("Controller renamed");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    saveButton.disabled = false;
  }
});

loadController();
