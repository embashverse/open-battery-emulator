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
const SOC_GREEN_ABOVE_PCT = 50; // SOC bar: green above this,
const SOC_RED_BELOW_PCT = 20; // red below this, amber in between
const CURRENT_STEP_A = 1; // what one press of - or + changes
const SEND_PAUSE_MS = 300; // after the last - or + press, then the current is sent
const CELL_MIN_PX = 10; // cell squares never get smaller; the grid scrolls instead
const CELL_MAX_PX = 24;
const CONFIRM_MS = 3000; // how long a confirmation stays before it fades

// Explanations shown on hover or tap.
const TIPS = {
  FULL: "a cell reached its maximum voltage, charging is blocked",
  EMPTY: "a cell reached its minimum voltage, discharging is blocked",
};
const CAUSED = {
  FULL: "caused FULL: charging is blocked",
  EMPTY: "caused EMPTY: discharging is blocked",
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

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

// --- 3. Messages -------------------------------------------------------------
// One place for every message, under the header, on both tabs. An error
// stays until it is closed; a confirmation fades out after CONFIRM_MS. The
// same error text is never shown twice at once.

// Shows a message. kind is "error" or "ok". action, if given, is
// {label, onClick} for a button in the message (like Reload). Returns the
// message element, so it can be removed by code too.
function notify(text, kind, action) {
  const box = document.getElementById("messages");
  const already = Array.from(box.children).find(function (m) {
    return m.dataset.text === text;
  });
  if (already) already.remove(); // shown again: the newest one counts

  const message = document.createElement("div");
  message.className = "message " + kind;
  message.dataset.text = text;
  const words = document.createElement("span");
  words.className = "text";
  words.textContent = text;
  message.appendChild(words);

  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = action.label;
    button.addEventListener("click", action.onClick);
    message.appendChild(button);
  }
  if (kind === "error" || action) {
    const close = document.createElement("button");
    close.type = "button";
    close.className = "close";
    close.setAttribute("aria-label", "Close");
    close.textContent = "×";
    close.addEventListener("click", function () {
      message.remove();
    });
    message.appendChild(close);
  } else {
    setTimeout(function () {
      message.classList.add("fading");
      setTimeout(function () { message.remove(); }, 400); // after the fade
    }, CONFIRM_MS);
  }
  box.appendChild(message);
  return message;
}

// The result of a command: a short confirmation, or the error.
function notifyResult(result, okText) {
  if (result.ok) {
    notify(okText || result.message, "ok");
  } else {
    notify(result.message, "error");
  }
}

// --- 4. Tooltips (hover or tap) ----------------------------------------------
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

// --- 5. Header: connection and the run button --------------------------------

let runState = null; // "RUNNING" or "STOPPED", from the latest state
let connected = false;
let busy = false; // a Play or Stop is on its way
let shownError = null; // the failed Play error already shown, so it shows once

function showConnection(ok) {
  connected = ok;
  const badge = document.getElementById("connection");
  badge.textContent = ok ? "Connected" : "No connection";
  badge.classList.toggle("lost", !ok);
  showRunButton();
}

// Left part: the state (green dot "Running" or grey dot "Stopped"). Right
// part: what pressing does ("Play" or "Stop"), or "Starting…" / "Stopping…"
// while the command is on its way.
function showRunButton() {
  const running = runState === "RUNNING";
  const button = document.getElementById("run-button");
  button.classList.toggle("running", running);
  button.querySelector(".symbol").className = "symbol " + (running ? "stop" : "play");
  button.disabled = busy || !connected || runState === null;
  button.setAttribute("aria-label", running ? "Running, press to stop" : "Stopped, press to play");

  setText("run-label", running ? "Running" : "Stopped");
  let verb = running ? "Stop" : "Play";
  if (busy) verb = running ? "Stopping…" : "Starting…";
  setText("run-verb", verb);
}

function showHeader(state) {
  runState = state.run_state;
  showRunButton();

  // A failed Play is shown once; closed, it comes back only for a new error.
  if (state.error && state.run_state === "STOPPED" && state.error !== shownError) {
    notify("Could not start: " + state.error, "error");
  }
  shownError = state.error;

  // Editing is only possible while STOPPED (REQ-16), live controls only
  // while RUNNING.
  const running = state.run_state === "RUNNING";
  document.getElementById("config-fields").disabled = running;
  document.getElementById("save").disabled = running || saving;
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
  // A failed Play also shows up as state.error; this shows it at once.
  if (!result.ok) {
    shownError = result.message;
    notify("Could not start: " + result.message, "error");
  }
}

// --- 6. Tabs -----------------------------------------------------------------
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

// --- 7. Live values ----------------------------------------------------------

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

  const soc = result.soc_pct;
  setText("soc", soc.toFixed(2) + " %");
  const bar = document.getElementById("soc-bar");
  bar.style.width = Math.max(0, Math.min(100, soc)) + "%";
  bar.classList.toggle("red", soc < SOC_RED_BELOW_PCT);
  bar.classList.toggle("amber", soc >= SOC_RED_BELOW_PCT && soc <= SOC_GREEN_ABOVE_PCT);

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

// --- 8. Cell grid ------------------------------------------------------------
// One row per module, one small square per cell, grouped by string with a
// label above each string. Calm green by default: amber for a cell more than
// CELL_DEVIATION_WARN_V from the pack average, red for a cell at or beyond
// its minimum or maximum voltage, and for the cell that caused FULL or EMPTY.
// The lowest and highest cell get an outline. Built once, updated each poll.

let gridShape = ""; // "strings x modules x cells" of the squares on the page
let gridModules = 0;
let gridStrings = 0;
let gridColumns = 0;

function buildGrid(modules, strings) {
  const grid = document.getElementById("grid");
  const cellsPerModule = modules[0].length;
  const modulesPerString = modules.length / strings;
  grid.innerHTML = "";
  grid.style.gridTemplateColumns = "auto repeat(" + cellsPerModule + ", var(--cell-size))";
  modules.forEach(function (cells, m) {
    if (m % modulesPerString === 0) {
      const label = document.createElement("span");
      label.className = "string-label";
      label.textContent = "String " + (m / modulesPerString + 1);
      grid.appendChild(label);
    }
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
  gridShape = strings + "x" + modules.length + "x" + cellsPerModule;
  gridModules = modules.length;
  gridStrings = strings;
  gridColumns = cellsPerModule;
  sizeCells();
}

// The squares share the space the card has left, between CELL_MIN_PX and
// CELL_MAX_PX. A pack too large for that scrolls inside the card.
function sizeCells() {
  if (gridModules === 0) return;
  const area = document.querySelector(".grid-scroll");
  if (area.clientHeight === 0) return; // tab hidden
  const gap = 3;
  const padding = 4;
  const labelWidth = 34; // the "M16" column
  const rows = gridModules + gridStrings; // module rows and string label rows
  // The "String n" rows, measured on the page (height plus the gap above).
  let labels = 0;
  document.querySelectorAll("#grid .string-label").forEach(function (label) {
    labels += label.offsetHeight + parseFloat(getComputedStyle(label).marginTop);
  });
  const byHeight = (area.clientHeight - padding - labels - gap * (rows - 1)) / gridModules;
  const byWidth = (area.clientWidth - padding - labelWidth - gap * gridColumns) / gridColumns;
  let size = Math.max(CELL_MIN_PX, Math.min(CELL_MAX_PX, Math.floor(Math.min(byHeight, byWidth))));
  const grid = document.getElementById("grid");
  grid.style.setProperty("--cell-size", size + "px");
  // Labels can make rows a pixel taller than the estimate: shrink until it
  // really fits (below CELL_MIN_PX the card scrolls instead).
  while (size > CELL_MIN_PX && area.scrollHeight > area.clientHeight) {
    size -= 1;
    grid.style.setProperty("--cell-size", size + "px");
  }
}

function showGrid(state) {
  const result = state.result;
  if (result === null) return;
  const modules = result.module_cell_voltages;
  const strings = result.strings.length;
  if (strings + "x" + modules.length + "x" + modules[0].length !== gridShape) {
    buildGrid(modules, strings);
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
  // The cell that caused FULL (highest) or EMPTY (lowest), if any.
  let culprit = null;
  if (result.state === "FULL") culprit = high;
  if (result.state === "EMPTY") culprit = low;
  const socs = state.module_cell_socs;

  document.querySelectorAll("#grid .cell").forEach(function (box) {
    const m = Number(box.dataset.module);
    const c = Number(box.dataset.cell);
    const voltage = modules[m - 1][c - 1];
    const caused = culprit !== null && m === culprit.module && c === culprit.cell;
    const atLimit =
      (state.cell_v_min !== null && voltage <= state.cell_v_min) ||
      (state.cell_v_max !== null && voltage >= state.cell_v_max);
    const farOff = Math.abs(voltage - average) > CELL_DEVIATION_WARN_V;
    const extreme = (m === low.module && c === low.cell) || (m === high.module && c === high.cell);
    box.classList.toggle("red", atLimit || caused);
    box.classList.toggle("amber", farOff && !atLimit && !caused);
    box.classList.toggle("extreme", extreme);

    let tip = "Module " + m + ", cell " + c + ": " + voltage.toFixed(3) + " V";
    if (socs) tip += ", SOC " + socs[m - 1][c - 1].toFixed(1) + " %";
    if (caused) tip += " (" + CAUSED[result.state] + ")";
    box.dataset.tip = tip;
  });
}

// --- 9. Live controls --------------------------------------------------------
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
  const result = await sendCommand(command, value);
  notifyResult(result, "Applied");
  return result;
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
  // Messages coming and going change the space the grid has.
  new ResizeObserver(sizeCells).observe(document.querySelector(".grid-scroll"));
  setText("warn-mv", (CELL_DEVIATION_WARN_V * 1000).toFixed(0));
}

// An empty or unreadable field is sent as text, so the engine explains it.
// Number() also reads hex like "0x200".
function readNumber(input) {
  const text = input.value;
  const number = Number(text);
  return text.trim() === "" || Number.isNaN(number) ? text : number;
}

// --- 10. Config form ---------------------------------------------------------
// One input per field from GET /api/config. The group is the first part of
// the path (pack.cells_per_module -> Pack). Under Interfaces, each interface
// gets its own heading, whatever its name.

let loadedConfig = null; // the answer of GET /api/config the form was built from
const KNOWN_GROUPS = ["battery", "pack", "start", "variation", "current", "interfaces", "webui"];

async function loadConfig() {
  try {
    loadedConfig = await getJson("/api/config");
  } catch (error) {
    notify("Cannot read config.yaml: " + error.message, "error");
    return;
  }
  buildForm(loadedConfig);
  hideReloadNotice();
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

// --- 11. Saving and noticing a changed file ----------------------------------

let saving = false; // a Save is on its way
let reloadNotice = null; // the "config.yaml changed" message, while shown
let reloadNoticeVersion = null; // the file version it was shown for

async function saveConfig(event) {
  event.preventDefault();
  const changes = {};
  document.querySelectorAll(".setting").forEach(function (input) {
    if (!input.disabled && isChanged(input)) {
      changes[input.field.path] = formValue(input);
    }
  });
  if (Object.keys(changes).length === 0) {
    notify("Nothing changed.", "ok");
    return;
  }

  const button = document.getElementById("save");
  saving = true;
  button.disabled = true;
  button.textContent = "Saving…";
  const result = await sendCommand("save_config", {
    version: loadedConfig.version,
    changes: changes,
  });
  saving = false;
  button.disabled = runState === "RUNNING";
  button.textContent = "Save";
  if (result.ok) {
    await loadConfig(); // the form now shows the saved file
    notify(result.message, "ok");
  } else {
    notify(result.message, "error");
    checkConfigFile(); // shows the reload notice if the file changed
  }
}

// Shows the reload notice when config.yaml is no longer the version the form
// was built from (REQ-17). Never reloads by itself: that would lose edits.
async function checkConfigFile() {
  if (loadedConfig === null) return;
  let now;
  try {
    now = await getJson("/api/config");
  } catch (error) {
    return; // the connection indicator already shows a lost connection
  }
  if (now.version === loadedConfig.version) {
    hideReloadNotice();
  } else if (now.version !== reloadNoticeVersion) {
    // Once per new version of the file: closed, it stays closed until the
    // file changes again.
    reloadNotice = notify("config.yaml changed since this page loaded it.", "ok", {
      label: "Reload",
      onClick: loadConfig,
    });
    reloadNoticeVersion = now.version;
  }
}

function hideReloadNotice() {
  if (reloadNotice !== null) reloadNotice.remove();
  reloadNotice = null;
  reloadNoticeVersion = null;
}

function setUpConfig() {
  document.getElementById("config-form").addEventListener("submit", saveConfig);
  setInterval(checkConfigFile, CONFIG_CHECK_MS);
  loadConfig();
}

// --- 12. The polling loop ----------------------------------------------------

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
