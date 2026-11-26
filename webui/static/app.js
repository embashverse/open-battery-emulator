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
const CELL_MIN_PX = 12; // cell squares never get smaller; the grid scrolls instead
const CELL_MAX_PX = 30;
const CONFIRM_MS = 3000; // how long a confirmation stays before it fades
const MAX_MESSAGES = 3; // newest messages kept per area, older ones go

// Explanations shown on hover or tap.
const TIPS = {
  FULL: "a cell reached its maximum voltage, charging is blocked",
  EMPTY: "a cell reached its minimum voltage, discharging is blocked",
};
// The pack state in normal case. FULL and EMPTY stay as they are.
const STATE_NAMES = { IDLE: "Idle", CHARGING: "Charging", DISCHARGING: "Discharging" };
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
// Messages appear next to the button that caused them: under the run button
// (area "live") or under Save (area "config"), so the page never shifts. An
// error stays until it is closed; a confirmation fades out after CONFIRM_MS.
// The same text is never shown twice at once, and only the newest
// MAX_MESSAGES stay in each area. Under Save the newest is on top, under the
// run button at the bottom.

// Shows a message. kind is "error" or "ok", area "live" or "config".
// action, if given, is {label, onClick} for a button in the message (like
// Reload). Returns the message element, so it can be removed by code too.
function notify(text, kind, area, action) {
  const box = document.getElementById(area + "-messages");
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
  if (area === "config") {
    // The Save card's slot shows about one message, so the newest goes on
    // top and the slot scrolls back up to it.
    box.prepend(message);
    while (box.children.length > MAX_MESSAGES) {
      box.lastElementChild.remove(); // the oldest
    }
    box.parentElement.scrollTop = 0;
  } else {
    box.appendChild(message);
    while (box.children.length > MAX_MESSAGES) {
      box.firstElementChild.remove(); // the oldest
    }
  }
  return message;
}

// The result of a command: a short confirmation, or the error.
function notifyResult(result, okText, area) {
  if (result.ok) {
    notify(okText || result.message, "ok", area);
  } else {
    notify(result.message, "error", area);
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

// --- 5. Connection and the run button --------------------------------

let runState = null; // "RUNNING" or "STOPPED", from the latest state
let connected = false;
let busy = false; // a Play or Stop is on its way
let shownError = null; // the failed Play error already shown, so it shows once

// The dot beats while connected.
function showConnection(ok) {
  connected = ok;
  const badge = document.getElementById("connection");
  setText("connection-text", ok ? "Connected" : "No connection");
  badge.classList.toggle("lost", !ok);
  badge.classList.toggle("alive", ok);
  showRunButton();
}

// The run button sits in the Live tab's right column. Left half: the state
// (beating green dot "Running", grey dot "Stopped", or "Starting…" /
// "Stopping…" while the command is on its way). Right half: what pressing
// does ("Play" or "Stop").
function showRunButton() {
  const running = runState === "RUNNING";
  const button = document.getElementById("run-button");
  button.classList.toggle("running", running);
  button.querySelector(".run-state").classList.toggle("alive", running && !busy);
  button.querySelector(".symbol").className = "symbol " + (running ? "stop" : "play");
  button.disabled = busy || !connected || runState === null;
  button.setAttribute("aria-label", running ? "Running, press to stop" : "Stopped, press to play");

  let label = running ? "Running" : "Stopped";
  if (busy) label = running ? "Stopping…" : "Starting…";
  setText("run-label", label);
  setText("run-verb", running ? "Stop" : "Play");
}

function showHeader(state) {
  runState = state.run_state;
  showRunButton();

  // A failed Play is shown once; closed, it comes back only for a new error.
  if (state.error && state.run_state === "STOPPED" && state.error !== shownError) {
    notify("Could not start: " + state.error, "error", "live");
  }
  shownError = state.error;

  // Editing is only possible while STOPPED (REQ-16), live controls only
  // while RUNNING.
  const running = state.run_state === "RUNNING";
  document.getElementById("config-fields").disabled = running;
  document.getElementById("live-controls").disabled = !running;
  showSaveState();
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
    notify("Could not start: " + result.message, "error", "live");
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

// For example "M11·C7".
function where(position) {
  return "M" + position.module + "\u00b7C" + position.cell;
}

// For example 4534 s -> "1:15:34".
function clock(seconds) {
  const whole = Math.floor(seconds);
  const h = Math.floor(whole / 3600);
  const m = Math.floor(whole / 60) % 60;
  const s = whole % 60;
  return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

// For example 2.57 h -> "2 h 34 min", 0.2 h -> "12 min".
function duration(hours) {
  const minutes = Math.round(hours * 60);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h > 0 ? h + " h " + m + " min" : m + " min";
}

// Time until empty (discharging) or full (charging), from the charge left
// and the current that really flows now. Empty at 0 A or when stopped.
function timeLeft(state, result) {
  const current = result.current_a;
  if (state.run_state !== "RUNNING" || current === 0 || !state.capacity_ah) return "";
  if (current > 0) {
    return "about " + duration((result.soc_pct / 100) * state.capacity_ah / current) + " to empty";
  }
  return "about " + duration(((100 - result.soc_pct) / 100) * state.capacity_ah / -current) + " to full";
}

function showValues(state) {
  const result = state.result;
  const running = state.run_state === "RUNNING";
  document.getElementById("tab-live").classList.toggle("stopped", !running);
  setText("elapsed", clock(state.elapsed_s));

  const stateBox = document.getElementById("state");
  if (result === null) {
    setText("state", running ? "Starting" : "–");
    return; // no tick has run yet
  }
  setText("state", STATE_NAMES[result.state] || result.state);
  stateBox.classList.toggle("charging", result.state === "CHARGING");
  const special = TIPS[result.state];
  stateBox.classList.toggle("attention", Boolean(special));
  if (special) {
    stateBox.dataset.tip = special;
  } else {
    delete stateBox.dataset.tip;
  }

  const soc = result.soc_pct;
  setText("soc", soc.toFixed(2) + " %");
  setText("eta", timeLeft(state, result));
  const fill = document.getElementById("soc-bar");
  fill.style.width = Math.max(0, Math.min(100, soc)) + "%";
  fill.classList.toggle("red", soc < SOC_RED_BELOW_PCT);
  fill.classList.toggle("amber", soc >= SOC_RED_BELOW_PCT && soc <= SOC_GREEN_ABOVE_PCT);
  // The light in the bar moves the way the charge goes.
  const bar = document.getElementById("bar");
  bar.classList.toggle("charging", running && result.current_a < 0);
  bar.classList.toggle("discharging", running && result.current_a > 0);

  setText("voltage", result.voltage_v.toFixed(2) + " V");
  setText("current", result.current_a.toFixed(1) + " A");

  const low = result.lowest_cell;
  const high = result.highest_cell;
  setText("low-v", low.voltage_v.toFixed(3) + " V");
  setText("low-where", where(low));
  setText("high-v", high.voltage_v.toFixed(3) + " V");
  setText("high-where", where(high));
  setText("spread", ((high.voltage_v - low.voltage_v) * 1000).toFixed(0) + " mV");
  document.getElementById("low-v").parentElement.dataset.tip =
    "Lowest cell: module " + low.module + ", cell " + low.cell;
  document.getElementById("high-v").parentElement.dataset.tip =
    "Highest cell: module " + high.module + ", cell " + high.cell;
  setText("charge-limit", result.max_charge_a.toFixed(0) + " A");
  setText("discharge-limit", result.max_discharge_a.toFixed(0) + " A");

  const timeout = document.getElementById("pill-timeout");
  timeout.hidden = !result.current_timeout;
  const timeoutMs = state.timeout_ms === null ? "the set time" : state.timeout_ms + " ms";
  timeout.dataset.tip = "no current command received for " + timeoutMs + ", current set to 0 A";
  document.getElementById("pill-temp").hidden = !result.temp_out_of_range;
}

// --- 8. Cell grid ------------------------------------------------------------
// Strings side by side, each a block with its label above and one row per
// module, one small square per cell. Calm green by default: amber for a cell
// more than CELL_DEVIATION_WARN_V from the pack average, red for a cell at or
// beyond its minimum or maximum voltage, and for the cell that caused FULL or
// EMPTY. The lowest and highest cell get an outline. Built once, updated each
// poll.

let gridShape = ""; // "strings x modules x cells" of the squares on the page

function buildGrid(modules, strings) {
  const grid = document.getElementById("grid");
  const cellsPerModule = modules[0].length;
  const modulesPerString = modules.length / strings;
  grid.innerHTML = "";
  let block = null;
  modules.forEach(function (cells, m) {
    if (m % modulesPerString === 0) {
      block = document.createElement("div");
      block.className = "string";
      block.style.gridTemplateColumns = "30px repeat(" + cellsPerModule + ", var(--size))";
      const label = document.createElement("span");
      label.className = "string-label";
      label.textContent = "String " + (m / modulesPerString + 1);
      block.appendChild(label);
      grid.appendChild(block);
    }
    const label = document.createElement("span");
    label.className = "module-label";
    label.textContent = "M" + (m + 1);
    block.appendChild(label);
    cells.forEach(function (_, c) {
      const box = document.createElement("div");
      box.className = "cell";
      box.dataset.module = m + 1;
      box.dataset.cell = c + 1;
      block.appendChild(box);
    });
  });
  gridShape = strings + "x" + modules.length + "x" + cellsPerModule;
  sizeCells();
}

// Picks the square size. The card's width decides it, up to CELL_MAX_PX
// (strings wrap to a new row of blocks when they do not fit side by side);
// the card is then only as tall as its rows. Only when that grid would be
// taller than the room left on screen do the squares shrink, down to
// CELL_MIN_PX; below that the grid scrolls inside its card. Gaps grow a
// little with the squares.
function sizeCells() {
  const area = document.querySelector(".grid-area");
  const grid = document.getElementById("grid");
  if (gridShape === "" || area.clientWidth === 0) return; // no pack yet, or tab hidden
  const room = gridRoom(area);
  for (let size = CELL_MAX_PX; size >= CELL_MIN_PX; size -= 1) {
    grid.style.setProperty("--size", size + "px");
    grid.style.setProperty("--gap", Math.max(2, Math.round(size * 0.18)) + "px");
    const fits = area.scrollWidth <= area.clientWidth && grid.offsetHeight <= room;
    if (fits) return;
  }
}

// The most height the grid may take: the left column minus the summary row,
// the gap and the rest of the card (padding, heading, legend). On a phone the
// page scrolls, so there is no limit (the same 900 px as in style.css).
function gridRoom(area) {
  if (!window.matchMedia("(min-width: 900px)").matches) return Infinity;
  const column = document.querySelector(".live-main");
  const card = document.querySelector(".grid-card");
  const summary = document.querySelector(".summary-row");
  const gap = parseFloat(getComputedStyle(column).rowGap) || 0;
  const cardRest = card.offsetHeight - area.offsetHeight;
  return column.clientHeight - summary.offsetHeight - gap - cardRest;
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

let socTouched = false; // the slider was moved and not applied yet
let sendTimer = null; // waits SEND_PAUSE_MS after the last change of the current
let direction = "discharge"; // simulated current: "discharge" or "charge"
let shownSimulatedA = null; // the engine's simulated current last put in the controls

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

function showDirection() {
  document.querySelectorAll("#direction .segment").forEach(function (button) {
    button.classList.toggle("selected", button.dataset.direction === direction);
  });
}

// Simulated current: the sign of the engine's value sets the direction, its
// size the amps (0 keeps the chosen direction). Updated only when the engine's
// value changes (first state, a Play, another browser) and nothing is waiting
// to be sent from here.
function showSimulatedCurrent(state) {
  document.getElementById("sim-current").disabled = state.current_source !== "simulated";
  if (state.simulated_a === shownSimulatedA || sendTimer !== null) return;
  const input = document.getElementById("current-input");
  if (document.activeElement === input) return;
  shownSimulatedA = state.simulated_a;
  if (state.simulated_a > 0) direction = "discharge";
  if (state.simulated_a < 0) direction = "charge";
  input.value = Math.abs(state.simulated_a);
  showDirection();
}

function showControls(state) {
  showSources(state);
  showSimulatedCurrent(state);

  const hold = document.getElementById("hold");
  if (document.activeElement !== hold) {
    hold.checked = state.hold_soc;
  }

  const slider = document.getElementById("soc-input");
  if (!socTouched && state.result !== null) {
    slider.value = Math.round(state.result.soc_pct);
    showSliderValue();
  }
}

function showSliderValue() {
  setText("soc-input-value", document.getElementById("soc-input").value + " %");
}

async function sendLive(command, value) {
  const result = await sendCommand(command, value);
  notifyResult(result, "Applied", "live");
  return result;
}

// Positive for discharge, negative for charge. An empty or unreadable field
// is sent as it is, so the engine explains it.
function sendCurrent() {
  sendTimer = null;
  const amps = readNumber(document.getElementById("current-input"));
  if (typeof amps !== "number") {
    sendLive("set_simulated_current", amps);
    return;
  }
  const value = direction === "charge" ? -Math.abs(amps) : Math.abs(amps);
  shownSimulatedA = value; // already shown, do not refill from the next state
  sendLive("set_simulated_current", value);
}

// Every change waits SEND_PAUSE_MS, then the current is sent once.
function currentChanged() {
  clearTimeout(sendTimer);
  sendTimer = setTimeout(sendCurrent, SEND_PAUSE_MS);
}

function stepCurrent(change) {
  const input = document.getElementById("current-input");
  const now = Number(input.value) || 0;
  input.value = Math.max(0, Math.round((now + change) * 1000) / 1000); // no 0.30000000000000004
  currentChanged();
}

function setUpControls() {
  document.getElementById("source").addEventListener("click", function (event) {
    const button = event.target.closest(".segment");
    if (!button || button.getAttribute("aria-disabled") === "true") return; // the tip explains
    sendLive("set_source", button.dataset.name);
  });

  document.getElementById("direction").addEventListener("click", function (event) {
    const button = event.target.closest(".segment");
    if (!button || button.dataset.direction === direction) return;
    direction = button.dataset.direction;
    showDirection();
    currentChanged();
  });
  document.getElementById("current-input").addEventListener("input", currentChanged);
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
  // A resized window, or the Live tab shown again, changes the grid's space.
  // The left column is watched, not the card: the card's size follows the grid.
  new ResizeObserver(sizeCells).observe(document.querySelector(".live-main"));
  setText("warn-mv", (CELL_DEVIATION_WARN_V * 1000).toFixed(0));
  showDirection();
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
// gets its own tab, whatever its name, and only the selected one is shown.

let loadedConfig = null; // the answer of GET /api/config the form was built from
let selectedInterface = null; // the open interface tab, kept while the page is open
const KNOWN_GROUPS = ["battery", "pack", "start", "variation", "current", "interfaces", "webui"];

async function loadConfig() {
  try {
    loadedConfig = await getJson("/api/config");
  } catch (error) {
    notify("Cannot read config.yaml: " + error.message, "error", "config");
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

  let page = null; // the box of the interface being filled
  config.fields.forEach(function (field) {
    const group = groupOf(field.path);
    const section = document.querySelector('[data-group="' + group + '"]');
    const box = section.querySelector(".fields");
    section.hidden = false;
    let label = field.path.split(".").slice(1).join(".") || field.path;

    let target = box;
    if (group === "interfaces") {
      const name = field.path.split(".")[1];
      if (page === null || page.dataset.interface !== name) {
        page = document.createElement("div");
        page.className = "interface-page";
        page.dataset.interface = name;
        box.appendChild(page);
      }
      target = page;
      label = field.path.split(".").slice(2).join(".");
    }
    if (config.restart_needed.includes(field.path)) {
      label += " (needs restart)";
    }
    target.appendChild(makeInput(field, label, config));
  });

  buildInterfaceTabs();
  fillSourceChoices();
  showPackSummary();
  showSaveState();
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
    showSaveState();
    if (field.path.startsWith("pack.") || field.path === "battery") showPackSummary();
    if (field.path.startsWith("interfaces.") && field.path.endsWith(".enabled")) {
      fillSourceChoices();
      showInterfaceTabs();
    }
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

// Interface tabs: one per interface box made by buildForm(), in file order.
// They are spans, not buttons, so they still work while running, when the
// fieldset around the form disables every button in it.
function buildInterfaceTabs() {
  const tabs = document.getElementById("interface-tabs");
  tabs.innerHTML = "";
  const names = Array.from(document.querySelectorAll(".interface-page")).map(function (page) {
    return page.dataset.interface;
  });
  if (!names.includes(selectedInterface)) selectedInterface = names[0] || null;
  names.forEach(function (name) {
    const tab = document.createElement("span");
    tab.className = "segment";
    tab.setAttribute("role", "tab");
    tab.tabIndex = 0;
    tab.dataset.interface = name;
    const dot = document.createElement("span");
    dot.className = "dot";
    tab.appendChild(dot);
    tab.appendChild(document.createTextNode(name));
    tabs.appendChild(tab);
  });
  showInterfaceTabs();
}

// Shows the selected interface's fields, and a green dot on every tab whose
// enabled box is ticked in the form (grey when not).
function showInterfaceTabs() {
  document.querySelectorAll("#interface-tabs .segment").forEach(function (tab) {
    const name = tab.dataset.interface;
    const selected = name === selectedInterface;
    tab.classList.toggle("selected", selected);
    tab.setAttribute("aria-selected", selected ? "true" : "false");
    const enabled = settingInput("interfaces." + name + ".enabled");
    tab.querySelector(".dot").classList.toggle("on", Boolean(enabled && enabled.checked));
  });
  document.querySelectorAll(".interface-page").forEach(function (page) {
    page.classList.toggle("shown", page.dataset.interface === selectedInterface);
  });
}

function selectInterface(event) {
  const tab = event.target.closest("#interface-tabs .segment");
  if (!tab) return;
  selectedInterface = tab.dataset.interface;
  showInterfaceTabs();
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

// The settings changed in the form and not saved yet: {path: value}.
function unsavedChanges() {
  const changes = {};
  document.querySelectorAll(".setting").forEach(function (input) {
    if (!input.disabled && isChanged(input)) {
      changes[input.field.path] = formValue(input);
    }
  });
  return changes;
}

// The Save card: how many changes wait, or why it is locked. The button is
// faded when there is nothing to save, while running and while saving.
function showSaveState() {
  const running = runState === "RUNNING";
  const count = running ? 0 : Object.keys(unsavedChanges()).length;
  const status = document.getElementById("save-status");
  let text = "No unsaved changes";
  if (running) text = "Press Stop to edit settings.";
  else if (count === 1) text = "1 unsaved change";
  else if (count > 1) text = count + " unsaved changes";
  status.textContent = text;
  status.classList.toggle("changed", count > 0);
  document.getElementById("save").disabled = running || saving || count === 0;
  setText("save-label", saving ? "Saving…" : "Save");
}

async function saveConfig(event) {
  event.preventDefault();
  const changes = unsavedChanges();
  if (Object.keys(changes).length === 0) {
    notify("Nothing changed.", "ok", "config");
    return;
  }

  saving = true;
  showSaveState();
  const result = await sendCommand("save_config", {
    version: loadedConfig.version,
    changes: changes,
  });
  saving = false;
  showSaveState();
  if (result.ok) {
    await loadConfig(); // the form now shows the saved file
    notify(result.message, "ok", "config");
  } else {
    notify(result.message, "error", "config");
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
    reloadNotice = notify("config.yaml changed since this page loaded it.", "ok", "config", {
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
  const tabs = document.getElementById("interface-tabs");
  tabs.addEventListener("click", selectInterface);
  tabs.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault(); // Space would scroll, Enter would submit
    selectInterface(event);
  });
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
