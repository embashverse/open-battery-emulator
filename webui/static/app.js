// Battery Emulator web UI. Plain JavaScript, no framework, no build step.
// Asks the server for the latest state about twice a second and sends
// commands when a control is used. The engine applies them at its next pass
// and reports each result back in state.actions, under the command's id.
//
// The Config tab is built from whatever config.yaml holds (GET /api/config),
// and the current sources from state.source_options, so nothing here knows
// any interface by name (REQ-18).

"use strict";

// --- 1. Settings -------------------------------------------------------------

const POLL_MS = 500;
const CONFIG_CHECK_MS = 2000; // how often to look for a changed config.yaml
const CELL_DEVIATION_WARN_V = 0.050; // amber when a cell is this far from the pack average
const CURRENT_STEP_A = 1; // what one press of - or + changes
const SEND_PAUSE_MS = 300; // after the last - or + press, then the current is sent
const ROW_MIN_PX = 8; // cell grid rows never get smaller; the grid scrolls instead
const ROW_MAX_PX = 28;

// Explanations shown on hover or tap.
const TIPS = {
  FULL: "a cell reached its maximum voltage, charging is blocked",
  EMPTY: "a cell reached its minimum voltage, discharging is blocked",
};

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

// --- 3. Tooltips (hover or tap) ----------------------------------------------
// Any element with a data-tip attribute shows that text in one floating box:
// on hover with a mouse, or on tap (tap again or anywhere else to close).
// The box is fixed to the window, so a scrolling card never cuts it off.

let tipTarget = null; // the element whose tip is shown
let tipPinned = false; // shown by a tap, stays until the next tap

function showTip(element) {
  const tip = document.getElementById("tip");
  tipTarget = element;
  tip.textContent = element.dataset.tip;
  tip.hidden = false;
  const box = element.getBoundingClientRect();
  const width = tip.offsetWidth;
  const height = tip.offsetHeight;
  let left = box.left + box.width / 2 - width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
  let top = box.top - height - 6; // above the element, or below if no room
  if (top < 8) top = box.bottom + 6;
  tip.style.left = left + "px";
  tip.style.top = top + "px";
}

function hideTip() {
  document.getElementById("tip").hidden = true;
  tipTarget = null;
  tipPinned = false;
}

// Called after every update, so a shown cell tip has the newest voltage.
function refreshTip() {
  if (tipTarget === null) return;
  if (!document.body.contains(tipTarget) || !tipTarget.dataset.tip || tipTarget.hidden) {
    hideTip();
  } else {
    showTip(tipTarget);
  }
}

function setUpTips() {
  document.addEventListener("mouseover", function (event) {
    const element = event.target.closest("[data-tip]");
    if (element && !tipPinned) showTip(element);
  });
  document.addEventListener("mouseout", function (event) {
    const element = event.target.closest("[data-tip]");
    if (element && element === tipTarget && !tipPinned) hideTip();
  });
  document.addEventListener("click", function (event) {
    const element = event.target.closest("[data-tip]");
    if (element && !(element === tipTarget && tipPinned)) {
      showTip(element);
      tipPinned = true;
    } else {
      hideTip();
    }
  });
  window.addEventListener("scroll", hideTip, true);
}

// --- 4. Header: connection and the run button --------------------------------

let runState = null; // "RUNNING" or "STOPPED", from the latest state
let connected = false;
let busy = false; // a Play or Stop is on its way

function showConnection(ok) {
  connected = ok;
  const badge = document.getElementById("connection");
  badge.textContent = ok ? "Connected" : "No connection";
  badge.classList.toggle("lost", !ok);
  showRunButton();
}

// RUNNING: green dot, "Running", stop symbol. STOPPED: grey dot, "Stopped",
// play symbol. While a command is on its way: "Starting…" or "Stopping…".
function showRunButton() {
  const running = runState === "RUNNING";
  const button = document.getElementById("run-button");
  button.classList.toggle("running", running);
  button.classList.toggle("busy", busy);
  button.querySelector(".symbol").className = "symbol " + (running ? "stop" : "play");
  button.disabled = busy || !connected || runState === null;
  button.setAttribute("aria-label", running ? "Running, press to stop" : "Stopped, press to play");

  let label = running ? "Running" : "Stopped";
  if (busy) label = running ? "Stopping…" : "Starting…";
  setText("run-label", label);
}

function showHeader(state) {
  runState = state.run_state;
  showRunButton();

  const error = document.getElementById("run-error");
  error.hidden = !(state.error && state.run_state === "STOPPED");
  error.textContent = state.error ? "Could not start: " + state.error : "";

  // Editing is only possible while STOPPED (REQ-16), live controls only
  // while RUNNING.
  const running = state.run_state === "RUNNING";
  document.getElementById("config-fields").disabled = running;
  document.getElementById("save").disabled = running;
  document.getElementById("config-lock").hidden = !running;
  document.getElementById("live-controls").disabled = !running;
}

async function playOrStop() {
  busy = true;
  showRunButton();
  const command = runState === "RUNNING" ? "stop" : "play";
  const result = await sendCommand(command, null);
  busy = false;
  showRunButton();
  if (!result.ok) {
    showMessage("message", result.message, "error");
  }
}

// --- 5. Tabs -----------------------------------------------------------------
// The open tab is kept in the address (#live or #config), so a reload keeps it.

function showTab(name) {
  document.querySelectorAll(".tab").forEach(function (button) {
    button.classList.toggle("selected", button.dataset.tab === name);
  });
  document.getElementById("tab-live").hidden = name !== "live";
  document.getElementById("tab-config").hidden = name !== "config";
  history.replaceState(null, "", "#" + name);
  sizeGridRows(); // the grid had no size while its tab was hidden
}

function setUpTabs() {
  document.querySelectorAll(".tab").forEach(function (button) {
    button.addEventListener("click", function () {
      showTab(button.dataset.tab);
    });
  });
  showTab(location.hash === "#config" ? "config" : "live");
}

// --- 6. Live values ----------------------------------------------------------

function where(position) {
  return "M" + position.module + " C" + position.cell;
}

function showValues(state) {
  const result = state.result;
  document.getElementById("tab-live").classList.toggle("stopped", state.run_state !== "RUNNING");
  setText("elapsed", state.elapsed_s.toFixed(1) + " s");
  setText("current-source", "from " + state.current_source);

  const stateBox = document.getElementById("state");
  if (result === null) {
    setText("state", state.run_state === "RUNNING" ? "starting" : "–");
    return; // no tick has run yet
  }
  setText("state", result.state);
  const special = TIPS[result.state];
  stateBox.classList.toggle("attention", Boolean(special));
  if (special) {
    stateBox.dataset.tip = special;
  } else {
    delete stateBox.dataset.tip;
  }

  setText("soc", result.soc_pct.toFixed(2) + " %");
  document.getElementById("soc-bar").style.width = Math.max(0, Math.min(100, result.soc_pct)) + "%";
  setText("voltage", result.voltage_v.toFixed(2) + " V");
  setText("current", result.current_a.toFixed(1) + " A");

  // For example "3.283 – 3.311 V  Δ 28 mV".
  const low = result.lowest_cell;
  const high = result.highest_cell;
  const spreadMv = (high.voltage_v - low.voltage_v) * 1000;
  setText(
    "cells-line",
    "Cells " + low.voltage_v.toFixed(3) + " – " + high.voltage_v.toFixed(3) +
      " V   Δ " + spreadMv.toFixed(0) + " mV"
  );
  setText("cells-where", "lowest " + where(low) + " · highest " + where(high));
  setText(
    "limits-line",
    "Charge limit " + result.max_charge_a.toFixed(1) + " A · discharge limit " +
      result.max_discharge_a.toFixed(1) + " A"
  );

  const timeout = document.getElementById("pill-timeout");
  timeout.hidden = !result.current_timeout;
  const timeoutMs = state.timeout_ms === null ? "the set time" : state.timeout_ms + " ms";
  timeout.dataset.tip = "no current command received for " + timeoutMs + ", current set to 0 A";
  document.getElementById("pill-temp").hidden = !result.temp_out_of_range;
}

// --- 7. Cell grid ------------------------------------------------------------
// One row per module, one box per cell. Neutral by default: amber for a cell
// more than CELL_DEVIATION_WARN_V from the pack average, red for a cell at or
// beyond its minimum or maximum voltage. The lowest and highest cell get an
// outline. The boxes are built once and updated on every poll.

let gridShape = ""; // "modules x cells" of the boxes on the page
let gridModules = 0;

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
      grid.appendChild(box);
    });
  });
  gridShape = modules.length + "x" + cellsPerModule;
  gridModules = modules.length;
  sizeGridRows();
}

// Rows share the height the card has left, between ROW_MIN_PX and
// ROW_MAX_PX. A pack too large for that scrolls inside the card.
function sizeGridRows() {
  if (gridModules === 0) return;
  const area = document.querySelector(".grid-scroll");
  if (area.clientHeight === 0) return; // tab hidden
  const gap = 3;
  const free = area.clientHeight - 4 - gap * (gridModules - 1); // 4 = grid padding
  const row = Math.max(ROW_MIN_PX, Math.min(ROW_MAX_PX, Math.floor(free / gridModules)));
  document.getElementById("grid").style.setProperty("--row-h", row + "px");
}

function showGrid(state) {
  const result = state.result;
  if (result === null) return;
  const modules = result.module_cell_voltages;
  if (modules.length + "x" + modules[0].length !== gridShape) {
    buildGrid(modules);
  }

  let sum = 0;
  let count = 0;
  modules.forEach(function (cells) {
    cells.forEach(function (voltage) {
      sum += voltage;
      count += 1;
    });
  });
  const average = sum / count;
  const low = result.lowest_cell;
  const high = result.highest_cell;

  document.querySelectorAll("#grid .cell").forEach(function (box) {
    const m = Number(box.dataset.module);
    const c = Number(box.dataset.cell);
    const voltage = modules[m - 1][c - 1];
    const atLimit =
      (state.cell_v_min !== null && voltage <= state.cell_v_min) ||
      (state.cell_v_max !== null && voltage >= state.cell_v_max);
    const farOff = Math.abs(voltage - average) > CELL_DEVIATION_WARN_V;
    const extreme = (m === low.module && c === low.cell) || (m === high.module && c === high.cell);
    box.classList.toggle("red", atLimit);
    box.classList.toggle("amber", farOff && !atLimit);
    box.classList.toggle("extreme", extreme);
    box.dataset.tip = "Module " + m + ", cell " + c + ": " + voltage.toFixed(3) + " V";
  });
}

// --- 8. Live controls --------------------------------------------------------
// The controls show the engine's live settings, but are never overwritten
// while you are using them.

let firstState = true;
let socTouched = false; // the slider was moved and not applied yet
let sendTimer = null; // waits SEND_PAUSE_MS after the last - or + press

// Fills a select with names (used by the Config tab).
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

// Current source: one segment per entry in state.source_options. One that
// cannot provide current right now is greyed and shows its reason on hover
// or tap. It is not a disabled button, so a tap still reaches it.
function showSources(state) {
  const box = document.getElementById("source");
  const key = JSON.stringify(state.source_options);
  if (box.dataset.key !== key) {
    box.innerHTML = "";
    state.source_options.forEach(function (option) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "segment";
      button.textContent = option.name;
      button.dataset.name = option.name;
      if (!option.available) {
        button.classList.add("unavailable");
        button.setAttribute("aria-disabled", "true");
        button.dataset.tip = option.reason;
      }
      box.appendChild(button);
    });
    box.dataset.key = key;
  }
  box.querySelectorAll(".segment").forEach(function (button) {
    button.classList.toggle("selected", button.dataset.name === state.current_source);
  });
}

function showControls(state) {
  showSources(state);

  const hold = document.getElementById("hold");
  if (document.activeElement !== hold) {
    hold.checked = state.hold_soc;
  }

  const currentInput = document.getElementById("current-input");
  if (firstState) {
    currentInput.value = state.simulated_a;
  }
  const slider = document.getElementById("soc-input");
  if (!socTouched && state.result !== null) {
    slider.value = Math.round(state.result.soc_pct);
    showSliderValue();
  }
  firstState = false;
}

function showSliderValue() {
  setText("soc-input-value", document.getElementById("soc-input").value + " %");
}

async function sendLive(command, value) {
  showMessage("message", "Sending…", "");
  const result = await sendCommand(command, value);
  showMessage("message", result.ok ? "Applied." : result.message, result.ok ? "ok" : "error");
}

function sendCurrent() {
  clearTimeout(sendTimer);
  sendLive("set_simulated_current", readNumber(document.getElementById("current-input")));
}

function stepCurrent(change) {
  const input = document.getElementById("current-input");
  const now = Number(input.value) || 0;
  input.value = Math.round((now + change) * 1000) / 1000; // no 0.30000000000000004
  clearTimeout(sendTimer);
  sendTimer = setTimeout(sendCurrent, SEND_PAUSE_MS);
}

function setUpControls() {
  document.getElementById("source").addEventListener("click", function (event) {
    const button = event.target.closest(".segment");
    if (!button || button.getAttribute("aria-disabled") === "true") return; // the tip explains
    sendLive("set_source", button.dataset.name);
  });

  document.getElementById("current-form").addEventListener("submit", function (event) {
    event.preventDefault();
    sendCurrent();
  });
  document.getElementById("current-minus").addEventListener("click", function () {
    stepCurrent(-CURRENT_STEP_A);
  });
  document.getElementById("current-plus").addEventListener("click", function () {
    stepCurrent(CURRENT_STEP_A);
  });

  const slider = document.getElementById("soc-input");
  slider.addEventListener("input", function () {
    socTouched = true;
    showSliderValue();
  });
  document.getElementById("soc-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    await sendLive("set_soc", Number(slider.value));
    socTouched = false;
  });

  document.getElementById("hold").addEventListener("change", function (event) {
    sendLive("hold_soc", event.target.checked);
  });

  document.getElementById("run-button").addEventListener("click", playOrStop);
  window.addEventListener("resize", sizeGridRows);
  setText("warn-mv", (CELL_DEVIATION_WARN_V * 1000).toFixed(0));
}

// An empty or unreadable field is sent as text, so the engine explains it.
// Number() also reads hex like "0x200".
function readNumber(input) {
  const text = input.value;
  const number = Number(text);
  return text.trim() === "" || Number.isNaN(number) ? text : number;
}

// --- 9. Config form ----------------------------------------------------------
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

// One labelled input: label on the left, input on the right. The input keeps
// its field, so Save can compare.
function makeInput(field, label, config) {
  const wrapper = document.createElement("label");
  wrapper.className = "field";
  const text = labelWithBreaks(label);
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

  wrapper.appendChild(text);
  wrapper.appendChild(input);
  return wrapper;
}

// A long name like module_frames.cells_per_frame may wrap after "_" or ".",
// never in the middle of a word.
function labelWithBreaks(label) {
  const text = document.createElement("span");
  label.split(/(?<=[._])/).forEach(function (part, i) {
    if (i > 0) text.appendChild(document.createElement("wbr"));
    text.appendChild(document.createTextNode(part));
  });
  return text;
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

// --- 10. Saving and noticing a changed file ----------------------------------

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

  showMessage("config-message", "Checking and saving…", "");
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
    // the connection indicator already shows a lost connection
  }
}

function setUpConfig() {
  document.getElementById("config-form").addEventListener("submit", saveConfig);
  document.getElementById("reload").addEventListener("click", loadConfig);
  setInterval(checkConfigFile, CONFIG_CHECK_MS);
  loadConfig();
}

// --- 11. The polling loop ----------------------------------------------------

async function poll() {
  try {
    const state = await getJson("/api/state");
    handleActions(state.actions);
    showConnection(true);
    showHeader(state);
    showValues(state);
    showGrid(state);
    showControls(state);
    refreshTip();
  } catch (error) {
    showConnection(false);
  }
  setTimeout(poll, POLL_MS);
}

setUpTips();
setUpTabs();
setUpControls();
setUpConfig();
poll();
