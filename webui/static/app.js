// Battery Emulator web UI. Plain JavaScript, no framework, no build step.
// Asks the server for the latest state about twice a second and sends
// commands when a control is used. The engine applies them at its next tick.

"use strict";

// --- 1. Settings -------------------------------------------------------------

const POLL_MS = 500;

// --- 2. Talking to the server ------------------------------------------------

async function fetchState() {
  const response = await fetch("/api/state", { cache: "no-store" });
  if (!response.ok) {
    throw new Error("server answered " + response.status);
  }
  return response.json();
}

// Sends one command. Shows the server's error message if it is refused.
async function sendCommand(command, value) {
  try {
    const response = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: command, value: value }),
    });
    const answer = await response.json();
    if (response.ok) {
      showMessage("Sent, applied at the next tick.", "ok");
    } else {
      showMessage(answer.error || "refused by the server", "error");
    }
  } catch (error) {
    showMessage("Could not reach the emulator: " + error.message, "error");
  }
}

function showMessage(text, kind) {
  const element = document.getElementById("message");
  element.textContent = text;
  element.className = "message " + kind;
}

// --- 3. Showing live values --------------------------------------------------

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

function cellText(position) {
  return position.voltage_v.toFixed(3) + " V (M" + position.module + " C" + position.cell + ")";
}

function showValues(state) {
  const result = state.result;
  setText("elapsed", state.elapsed_s.toFixed(1) + " s");
  if (result === null) {
    setText("state", "starting");
    return; // no tick has run yet
  }
  setText("state", result.state);
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

// --- 4. Cell grid ------------------------------------------------------------
// One row per module, one box per cell. Colour goes from blue (lowest voltage
// in the pack) to red (highest). The boxes are built once and recoloured on
// every update.

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
  const hue = 220 * (1 - fraction); // 220 = blue, 0 = red
  return "hsl(" + hue.toFixed(0) + ", 70%, 50%)";
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
    box.dataset.voltage = voltage;
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

// --- 5. Controls -------------------------------------------------------------
// The fields show the engine's live settings, but are never overwritten while
// you are using them.

let firstState = true;

function showControls(state) {
  const select = document.getElementById("source");
  const names = state.sources.join(",");
  if (select.dataset.names !== names) {
    select.innerHTML = "";
    state.sources.forEach(function (name) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    });
    select.dataset.names = names;
  }
  if (document.activeElement !== select) {
    select.value = state.current_source;
  }

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

function setUpControls() {
  document.getElementById("source").addEventListener("change", function (event) {
    sendCommand("set_source", event.target.value);
  });

  document.getElementById("current-form").addEventListener("submit", function (event) {
    event.preventDefault();
    sendCommand("set_simulated_current", readNumber("current-input"));
  });

  document.getElementById("soc-form").addEventListener("submit", function (event) {
    event.preventDefault();
    sendCommand("set_soc", readNumber("soc-input"));
  });

  document.getElementById("hold").addEventListener("change", function (event) {
    sendCommand("hold_soc", event.target.checked);
  });
}

// An empty or unreadable field is sent as text, so the server explains it.
function readNumber(id) {
  const text = document.getElementById(id).value;
  const number = Number(text);
  return text.trim() === "" || Number.isNaN(number) ? text : number;
}

// --- 6. The polling loop -----------------------------------------------------

async function poll() {
  const badge = document.getElementById("connection");
  try {
    const state = await fetchState();
    showValues(state);
    showGrid(state.result);
    showControls(state);
    badge.textContent = "live";
    badge.className = "badge ok";
  } catch (error) {
    badge.textContent = "no connection";
    badge.className = "badge";
  }
  setTimeout(poll, POLL_MS);
}

setUpControls();
poll();
