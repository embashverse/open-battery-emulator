// Battery Emulator web UI. Plain JavaScript, no framework, no build step.
// Asks the server for the latest state about twice a second and sends
// commands when a control is used. The engine applies them at its next pass
// and reports each result back in state.actions, under the command's id.
//
// The Config tab is built from whatever config.yaml holds (GET /api/config),
// so nothing here knows any interface by name (REQ-18).

"use strict";

// --- 1. Settings -------------------------------------------------------------

const POLL_MS = 500;
const CONFIG_CHECK_MS = 2000; // how often to look for a changed config.yaml

// --- 2. Talking to the server ------------------------------------------------

async function getJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  const answer = await response.json();
  if (!response.ok) {
    throw new Error(answer.error || "server answered " + response.status);
  }
  return answer;
}

// Sends one command and waits until the engine has applied it.
// Resolves to {ok, message}: refused at once by the server, or the engine's
// result from state.actions.
async function sendCommand(command, value) {
  let response;
  let answer;
  try {
    response = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: command, value: value }),
    });
    answer = await response.json();
  } catch (error) {
    return { ok: false, message: "Could not reach the emulator: " + error.message };
  }
  if (!response.ok) {
    return { ok: false, message: answer.error || "refused by the server" };
  }
  return waitForAction(answer.id);
}

// Results the page is waiting for: id -> function to call with the result.
const waiting = {};

function waitForAction(id) {
  return new Promise(function (resolve) {
    waiting[id] = resolve;
  });
}

function handleActions(actions) {
  actions.forEach(function (action) {
    if (waiting[action.id]) {
      waiting[action.id]({ ok: action.ok, message: action.message });
      delete waiting[action.id];
    }
  });
}

function showMessage(id, text, kind) {
  const element = document.getElementById(id);
  element.textContent = text;
  element.className = "message " + kind;
}

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

// --- 3. Header: run state and Play / Stop ------------------------------------

let runState = null; // "RUNNING" or "STOPPED", from the latest state
let busy = false; // a Play or Stop is on its way

function showHeader(state) {
  runState = state.run_state;
  const badge = document.getElementById("run-state");
  badge.textContent = state.run_state;
  badge.classList.toggle("running", state.run_state === "RUNNING");

  const button = document.getElementById("play-stop");
  button.textContent = state.run_state === "RUNNING" ? "Stop" : "Play";
  button.disabled = busy;

  const error = document.getElementById("run-error");
  error.hidden = !(state.error && state.run_state === "STOPPED");
  error.textContent = state.error ? "Could not start: " + state.error : "";

  // Editing is only possible while STOPPED (REQ-16), live controls only
  // while RUNNING.
  const running = state.run_state === "RUNNING";
  document.getElementById("config-fields").disabled = running;
  document.getElementById("config-lock").hidden = !running;
  document.getElementById("live-controls").disabled = !running;
}

async function playOrStop() {
  busy = true;
  document.getElementById("play-stop").disabled = true;
  const command = runState === "RUNNING" ? "stop" : "play";
  const result = await sendCommand(command, null);
  busy = false;
  if (!result.ok) {
    showMessage("message", result.message, "error");
  }
}

// --- 4. Tabs -----------------------------------------------------------------
// The open tab is kept in the address (#live or #config), so a reload keeps it.

function showTab(name) {
  document.querySelectorAll(".tab").forEach(function (button) {
    button.classList.toggle("selected", button.dataset.tab === name);
  });
  document.getElementById("tab-live").hidden = name !== "live";
  document.getElementById("tab-config").hidden = name !== "config";
  history.replaceState(null, "", "#" + name);
}

function setUpTabs() {
  document.querySelectorAll(".tab").forEach(function (button) {
    button.addEventListener("click", function () {
      showTab(button.dataset.tab);
    });
  });
  showTab(location.hash === "#config" ? "config" : "live");
}

// --- 5. Live values ----------------------------------------------------------

function cellText(position) {
  return position.voltage_v.toFixed(3) + " V (M" + position.module + " C" + position.cell + ")";
}

function showValues(state) {
  const result = state.result;
  document.querySelector(".tiles").classList.toggle("stopped", state.run_state !== "RUNNING");
  setText("elapsed", state.elapsed_s.toFixed(1) + " s");
  if (result === null) {
    setText("state", state.run_state === "RUNNING" ? "starting" : "-");
    return; // no tick has run yet
  }
  setText("state", result.state);
  document.getElementById("state").classList.toggle(
    "fault", result.state === "EMPTY" || result.state === "FULL"
  );
  setText("current", result.current_a.toFixed(1) + " A");
  setText("voltage", result.voltage_v.toFixed(2) + " V");
  setText("soc", result.soc_pct.toFixed(2) + " %");
  setText("lowest", cellText(result.lowest_cell));
  setText("highest", cellText(result.highest_cell));
  setText("max-charge", result.max_charge_a.toFixed(1) + " A");
  setText("max-discharge", result.max_discharge_a.toFixed(1) + " A");
  document.getElementById("flag-timeout").classList.toggle("on", result.current_timeout);
  document.getElementById("flag-temp").classList.toggle("on", result.temp_out_of_range);
}

// --- 6. Cell grid ------------------------------------------------------------
// One row per module, one box per cell, in shades of the accent colour: light
// for the lowest voltage in the pack, dark for the highest. The boxes are
// built once and recoloured on every update.

let gridShape = ""; // "modules x cells" of the boxes on the page
let selectedCell = null; // the box tapped last, its value is shown below

function buildGrid(modules) {
  const grid = document.getElementById("grid");
  const cellsPerModule = modules[0].length;
  grid.innerHTML = "";
  grid.style.gridTemplateColumns = "auto repeat(" + cellsPerModule + ", minmax(0, 1fr))";
  modules.forEach(function (cells, m) {
    const label = document.createElement("span");
    label.className = "module-label";
    label.textContent = "M" + (m + 1);
    grid.appendChild(label);
    cells.forEach(function (_, c) {
      const box = document.createElement("div");
      box.className = "cell";
      box.dataset.module = m + 1;
      box.dataset.cell = c + 1;
      box.addEventListener("click", function () {
        if (selectedCell) selectedCell.classList.remove("selected");
        selectedCell = box;
        box.classList.add("selected");
        showSelectedCell();
      });
      grid.appendChild(box);
    });
  });
  selectedCell = null;
  gridShape = modules.length + "x" + cellsPerModule;
}

function colourFor(voltage, lowest, highest) {
  const fraction = highest > lowest ? (voltage - lowest) / (highest - lowest) : 0.5;
  const lightness = 85 - 50 * fraction; // 85 % = light (lowest), 35 % = dark (highest)
  return "hsl(217, 70%, " + lightness.toFixed(0) + "%)"; // hue of the accent
}

function showGrid(result) {
  if (result === null) return;
  const modules = result.module_cell_voltages;
  if (modules.length + "x" + modules[0].length !== gridShape) {
    buildGrid(modules);
  }
  const lowest = result.lowest_cell.voltage_v;
  const highest = result.highest_cell.voltage_v;
  document.querySelectorAll("#grid .cell").forEach(function (box) {
    const voltage = modules[box.dataset.module - 1][box.dataset.cell - 1];
    box.style.background = colourFor(voltage, lowest, highest);
    box.title = "M" + box.dataset.module + " C" + box.dataset.cell + ": " + voltage.toFixed(3) + " V";
  });
  showSelectedCell();
}

function showSelectedCell() {
  if (selectedCell) {
    setText("cell-info", selectedCell.title);
  }
}

// --- 7. Live controls --------------------------------------------------------
// The fields show the engine's live settings, but are never overwritten while
// you are using them.

let firstState = true;

function fillSelect(select, names, value) {
  const key = names.join(",");
  if (select.dataset.names !== key) {
    select.innerHTML = "";
    names.forEach(function (name) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    });
    select.dataset.names = key;
  }
  if (document.activeElement !== select) {
    select.value = value;
  }
}

function showControls(state) {
  fillSelect(document.getElementById("source"), state.sources, state.current_source);

  const hold = document.getElementById("hold");
  if (document.activeElement !== hold) {
    hold.checked = state.hold_soc;
  }

  const currentInput = document.getElementById("current-input");
  currentInput.placeholder = "now " + state.simulated_a + " A";
  if (firstState) {
    currentInput.value = state.simulated_a;
    firstState = false;
  }
}

async function sendLive(command, value) {
  showMessage("message", "Sending...", "");
  const result = await sendCommand(command, value);
  showMessage("message", result.ok ? "Applied." : result.message, result.ok ? "ok" : "error");
}

function setUpControls() {
  document.getElementById("source").addEventListener("change", function (event) {
    sendLive("set_source", event.target.value);
  });

  document.getElementById("current-form").addEventListener("submit", function (event) {
    event.preventDefault();
    sendLive("set_simulated_current", readNumber(document.getElementById("current-input")));
  });

  document.getElementById("soc-form").addEventListener("submit", function (event) {
    event.preventDefault();
    sendLive("set_soc", readNumber(document.getElementById("soc-input")));
  });

  document.getElementById("hold").addEventListener("change", function (event) {
    sendLive("hold_soc", event.target.checked);
  });
}

// An empty or unreadable field is sent as text, so the engine explains it.
// Number() also reads hex like "0x200".
function readNumber(input) {
  const text = input.value;
  const number = Number(text);
  return text.trim() === "" || Number.isNaN(number) ? text : number;
}

// --- 8. Config form ----------------------------------------------------------
// One input per field from GET /api/config. The group is the first part of
// the path (pack.cells_per_module -> Pack). Under Interfaces, each interface
// gets its own heading, whatever its name.

let loadedConfig = null; // the answer of GET /api/config the form was built from
const KNOWN_GROUPS = ["battery", "pack", "start", "variation", "current", "interfaces", "webui"];

async function loadConfig() {
  try {
    loadedConfig = await getJson("/api/config");
  } catch (error) {
    showMessage("config-message", "Cannot read config.yaml: " + error.message, "error");
    return;
  }
  buildForm(loadedConfig);
  document.getElementById("reload-banner").hidden = true;
}

function groupOf(path) {
  const first = path.split(".")[0];
  return KNOWN_GROUPS.includes(first) ? first : "other";
}

function buildForm(config) {
  document.querySelectorAll("#config-form .fields").forEach(function (box) {
    box.innerHTML = "";
  });
  document.querySelector('[data-group="other"]').hidden = true;

  let lastInterface = null;
  config.fields.forEach(function (field) {
    const group = groupOf(field.path);
    const section = document.querySelector('[data-group="' + group + '"]');
    const box = section.querySelector(".fields");
    section.hidden = false;
    let label = field.path.split(".").slice(1).join(".") || field.path;

    if (group === "interfaces") {
      const name = field.path.split(".")[1];
      if (name !== lastInterface) {
        const heading = document.createElement("h3");
        heading.textContent = name;
        heading.style.gridColumn = "1 / -1";
        box.appendChild(heading);
        lastInterface = name;
      }
      label = field.path.split(".").slice(2).join(".");
    }
    if (config.restart_needed.includes(field.path)) {
      label += " (needs restart)";
    }
    box.appendChild(makeInput(field, label, config));
  });

  fillSourceChoices();
  showPackSummary();
  showMessage("config-message", "", "");
}

// One labelled input. The input keeps its field, so Save can compare.
function makeInput(field, label, config) {
  const wrapper = document.createElement("label");
  wrapper.className = "field";
  const text = document.createElement("span");
  text.textContent = label;
  let input;

  if (field.path === "battery") {
    input = document.createElement("select");
    const names = config.batteries.map(function (battery) { return battery.name; });
    if (!names.includes(field.value)) names.push(field.value);
    fillSelect(input, names, field.value);
  } else if (field.path === "current.source") {
    input = document.createElement("select"); // filled by fillSourceChoices()
    input.id = "config-source";
  } else if (field.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = field.value;
    wrapper.className = "field check";
  } else if (field.type === "int" || field.type === "float") {
    input = document.createElement("input");
    input.type = "number";
    input.step = "any";
    input.value = field.value;
  } else if (field.type === "hex") {
    input = document.createElement("input");
    input.type = "text";
    input.value = "0x" + field.value.toString(16).toUpperCase();
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.value = field.value === null ? "" : field.value;
    input.disabled = field.type === "other"; // lists and empty values: edit the file
  }

  input.field = field;
  input.classList.add("setting");
  input.addEventListener("input", function () {
    wrapper.classList.toggle("changed", isChanged(input));
    if (field.path.startsWith("pack.") || field.path === "battery") showPackSummary();
    if (field.path.startsWith("interfaces.") && field.path.endsWith(".enabled")) fillSourceChoices();
  });
  input.addEventListener("change", function () {
    input.dispatchEvent(new Event("input"));
  });

  if (field.type === "bool") {
    wrapper.appendChild(input);
    wrapper.appendChild(text);
  } else {
    wrapper.appendChild(text);
    wrapper.appendChild(input);
  }
  return wrapper;
}

function formValue(input) {
  if (input.type === "checkbox") return input.checked;
  if (input.tagName === "SELECT") return input.value;
  if (["int", "float", "hex"].includes(input.field.type)) return readNumber(input);
  return input.value;
}

function isChanged(input) {
  return formValue(input) !== input.field.value;
}

function settingInput(path) {
  return Array.from(document.querySelectorAll(".setting")).find(function (input) {
    return input.field.path === path;
  });
}

// Current source: "simulated" plus every interface ticked as enabled in the
// form. The saved value is kept even if it is not one of them, so the check
// at Save can explain it.
function fillSourceChoices() {
  const select = document.getElementById("config-source");
  if (!select) return;
  const names = ["simulated"];
  document.querySelectorAll(".setting").forEach(function (input) {
    const parts = input.field.path.split(".");
    if (parts[0] === "interfaces" && parts.length === 3 && parts[2] === "enabled" && input.checked) {
      names.push(parts[1]);
    }
  });
  const saved = select.field.value;
  const current = select.value || saved;
  if (!names.includes(saved)) names.push(saved);
  fillSelect(select, names, current);
}

// For example "96s2p · 192 cells · 316 V nominal · 200 Ah · 63.2 kWh".
function showPackSummary() {
  const read = function (path) {
    const input = settingInput(path);
    return input ? Number(formValue(input)) : NaN;
  };
  const cells = read("pack.cells_per_module");
  const modules = read("pack.modules_in_series");
  const strings = read("pack.strings_in_parallel");
  const summary = document.getElementById("pack-summary");
  if (![cells, modules, strings].every(Number.isInteger)) {
    summary.textContent = "";
    return;
  }
  const series = cells * modules;
  const parts = [series + "s" + strings + "p", series * strings + " cells"];

  const batteryInput = settingInput("battery");
  const name = batteryInput ? batteryInput.value : null;
  const battery = loadedConfig.batteries.find(function (b) { return b.name === name; });
  if (battery && battery.nominal_v !== null) {
    const volts = series * battery.nominal_v;
    const amphours = strings * battery.capacity_ah;
    parts.push(volts.toFixed(0) + " V nominal");
    parts.push(amphours.toFixed(0) + " Ah");
    parts.push((volts * amphours / 1000).toFixed(1) + " kWh");
  }
  summary.textContent = parts.join(" · ");
}

// --- 9. Saving and noticing a changed file -----------------------------------

async function saveConfig(event) {
  event.preventDefault();
  const changes = {};
  document.querySelectorAll(".setting").forEach(function (input) {
    if (!input.disabled && isChanged(input)) {
      changes[input.field.path] = formValue(input);
    }
  });
  if (Object.keys(changes).length === 0) {
    showMessage("config-message", "Nothing changed.", "");
    return;
  }

  showMessage("config-message", "Checking and saving...", "");
  const result = await sendCommand("save_config", {
    version: loadedConfig.version,
    changes: changes,
  });
  if (result.ok) {
    await loadConfig(); // the form now shows the saved file
    showMessage("config-message", result.message, "ok");
  } else {
    showMessage("config-message", result.message, "error");
    checkConfigFile(); // shows the reload banner if the file changed
  }
}

// Shows the reload banner when config.yaml is no longer the version the form
// was built from (REQ-17). Never reloads by itself: that would lose edits.
async function checkConfigFile() {
  if (loadedConfig === null) return;
  try {
    const now = await getJson("/api/config");
    document.getElementById("reload-banner").hidden = now.version === loadedConfig.version;
  } catch (error) {
    // the connection badge already shows a lost connection
  }
}

function setUpConfig() {
  document.getElementById("config-form").addEventListener("submit", saveConfig);
  document.getElementById("reload").addEventListener("click", loadConfig);
  document.getElementById("play-stop").addEventListener("click", playOrStop);
  setInterval(checkConfigFile, CONFIG_CHECK_MS);
  loadConfig();
}

// --- 10. The polling loop ----------------------------------------------------

async function poll() {
  const badge = document.getElementById("connection");
  try {
    const state = await getJson("/api/state");
    handleActions(state.actions);
    showHeader(state);
    showValues(state);
    showGrid(state.result);
    showControls(state);
    badge.textContent = "live";
    badge.className = "connection";
  } catch (error) {
    badge.textContent = "no connection";
    badge.className = "connection lost";
  }
  setTimeout(poll, POLL_MS);
}

setUpTabs();
setUpControls();
setUpConfig();
poll();
