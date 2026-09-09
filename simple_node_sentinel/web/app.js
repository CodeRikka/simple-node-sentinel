"use strict";

const $ = (selector) => document.querySelector(selector);
const metricNodes = new Map();
const charts = new Map();
const fanControls = new Map();
let gpuSignature = null;
let diskSignature = null;
let historyData = null;
let diskView = "physical";
let latestDisks = [];
let latestUsers = [];
let showSystemUsers = false;
let latestUserSettings = null;
let userSettingsEditing = false;
let openFanEditorUuid = null;

const COLORS = {
  blue: "var(--chart-blue)",
  teal: "var(--chart-teal)",
  violet: "var(--chart-violet)",
  coral: "var(--chart-coral)",
  slate: "var(--chart-slate)",
};

function formatBytes(value) {
  if (value === null || value === undefined) return "N/A";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let number = Number(value);
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) {
    number /= 1024;
    unit += 1;
  }
  return `${number.toFixed(unit < 2 ? 0 : 1)} ${units[unit]}`;
}

function formatPercent(value) {
  return value === null || value === undefined ? "N/A" : `${Number(value).toFixed(1)}%`;
}

function formatTemperature(value) {
  return value === null || value === undefined ? "N/A" : `${Number(value).toFixed(1)}°C`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "N/A";
  let value = Math.max(0, Math.floor(seconds));
  const days = Math.floor(value / 86400);
  value %= 86400;
  const hours = Math.floor(value / 3600);
  value %= 3600;
  const minutes = Math.floor(value / 60);
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hours || days) parts.push(`${hours}h`);
  parts.push(`${minutes}m`);
  return parts.join(" ");
}

function textElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function series(key, label, color, format = formatPercent, value = null) {
  return {
    key,
    label,
    color,
    format,
    value,
    normalize: (raw) => raw,
  };
}

function createMetricCard(key, title, subtitle, rows, chartDefinitions) {
  const element = textElement("article", "card", "");
  const heading = textElement("div", "card-heading", "");
  const headingText = textElement("div", "", "");
  headingText.append(
    textElement("h3", "", title),
    textElement("p", "card-subtitle", subtitle),
  );
  heading.appendChild(headingText);
  element.appendChild(heading);

  const primary = rows.find((row) => row.primary);
  if (primary) {
    const value = textElement("strong", "primary-value", "N/A");
    metricNodes.set(`${key}:${primary.id}`, value);
    element.appendChild(value);
  }

  const metrics = textElement("div", "metric-grid", "");
  rows.filter((row) => !row.primary).forEach((item) => {
    const row = textElement("div", item.id === "workloads" ? "metric workload-metric" : "metric", "");
    row.appendChild(textElement("span", "label", item.label));
    const value = textElement("strong", "", "N/A");
    metricNodes.set(`${key}:${item.id}`, value);
    row.appendChild(value);
    metrics.appendChild(row);
  });
  element.appendChild(metrics);

  const chartContainer = key.startsWith("gpu:")
    ? textElement("div", "gpu-history", "") : element;
  if (chartContainer !== element) element.appendChild(chartContainer);
  chartDefinitions.forEach((definition) => {
    const block = textElement("div", "chart-block", "");
    if (definition.title) block.appendChild(textElement("span", "chart-title", definition.title));
    const holder = textElement("div", "chart", "");
    block.appendChild(holder);
    chartContainer.appendChild(block);
    charts.set(
      `${key}:${definition.id}`,
      new window.TimeSeriesChart(holder, {
        title: `${title} ${definition.title || "history"}`,
        series: definition.series,
      }),
    );
  });
  rows.filter((row) => row.highlight).forEach((row) => {
    const value = metricNodes.get(`${key}:${row.id}`);
    const target = row.primary ? value : value.parentElement;
    target.tabIndex = 0;
    target.classList.add("chart-metric");
    const chart = charts.get(`${key}:${row.highlight[0]}`);
    target.addEventListener("pointerenter", () => chart.setExternalHighlight(row.highlight[1]));
    target.addEventListener("pointerleave", () => chart.setExternalHighlight(
      target.matches(":focus-visible") ? row.highlight[1] : null,
    ));
    target.addEventListener("focus", () => chart.setExternalHighlight(row.highlight[1]));
    target.addEventListener("blur", () => chart.setExternalHighlight(null));
  });
  return element;
}

function setMetric(cardKey, id, value) {
  const node = metricNodes.get(`${cardKey}:${id}`);
  if (node) node.textContent = value;
}

function clearCards(prefix) {
  for (const key of [...metricNodes.keys()]) {
    if (key.startsWith(`${prefix}:`)) metricNodes.delete(key);
  }
  for (const key of [...charts.keys()]) {
    if (key.startsWith(`${prefix}:`)) {
      charts.get(key).destroy();
      charts.delete(key);
    }
  }
  if (prefix === "gpu") {
    fanControls.forEach((nodes) => nodes.preview.resizeObserver?.disconnect());
    fanControls.clear();
  }
}

function fanColor(temperature) {
  if (temperature === null || temperature === undefined) return "#718096";
  const bounded = Math.max(60, Math.min(90, Number(temperature)));
  const hue = 130 * (90 - bounded) / 30;
  return `hsl(${hue} 72% 48%)`;
}

function createFanIcon() {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 64 64");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "GPU fan status");
  svg.classList.add("fan-icon");
  const rotor = document.createElementNS(namespace, "g");
  rotor.classList.add("fan-rotor");
  [
    "M32 28C22 25 18 15 23 8c7-7 15 2 12 15l-3 5Z",
    "M36 32c3-10 13-14 20-9 7 7-2 15-15 12l-5-3Z",
    "M32 36c10 3 14 13 9 20-7 7-15-2-12-15l3-5Z",
    "M28 32c-3 10-13 14-20 9-7-7 2-15 15-12l5 3Z",
  ].forEach((data) => {
    const path = document.createElementNS(namespace, "path");
    path.setAttribute("d", data);
    rotor.appendChild(path);
  });
  const hub = document.createElementNS(namespace, "circle");
  hub.setAttribute("cx", "32");
  hub.setAttribute("cy", "32");
  hub.setAttribute("r", "5");
  rotor.appendChild(hub);
  svg.appendChild(rotor);
  return svg;
}

function clonePoints(points) {
  return (points || []).map((point) => ({
    temperature_celsius: Number(point.temperature_celsius),
    fan_percent: Number(point.fan_percent),
  }));
}

function sortedPoints(points) {
  return clonePoints(points)
    .sort((left, right) => left.temperature_celsius - right.temperature_celsius);
}

function profileFromState(state) {
  return {
    minimum_percent: state.minimum_percent ?? 30,
    maximum_percent: state.maximum_percent ?? 100,
    cooldown_seconds: state.cooldown_seconds ?? state.idle_duration_seconds ?? 20,
    step_percent: state.step_percent ?? 5,
    curve_points: clonePoints(state.curve_points),
    revision: state.revision ?? 0,
  };
}

function drawFanCurve(svg, profile, options = {}) {
  svg.curveProfile = profile;
  svg.curveOptions = options;
  const namespace = "http://www.w3.org/2000/svg";
  svg.replaceChildren();
  const viewWidth = Math.max(240, svg.parentElement?.clientWidth || 640);
  svg.setAttribute("viewBox", `0 0 ${viewWidth} 160`);
  svg.classList.add("timeseries-svg");

  const left = 38;
  const right = 12;
  const top = 12;
  const bottom = 26;
  const width = viewWidth - left - right;
  const height = 160 - top - bottom;
  const minFan = Number(profile.minimum_percent) || 0;
  const maxFan = Number(profile.maximum_percent) || 100;
  const points = sortedPoints(profile.curve_points);
  const maxTemp = Math.max(100, ...points.map((point) => point.temperature_celsius), 80);

  for (const value of [0, 50, 100]) {
    const y = top + height - (value / 100) * height;
    const line = document.createElementNS(namespace, "line");
    line.setAttribute("x1", left);
    line.setAttribute("x2", left + width);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    line.classList.add("chart-grid");
    svg.appendChild(line);
    const mapped = minFan + (maxFan - minFan) * (value / 100);
    const label = document.createElementNS(namespace, "text");
    label.setAttribute("x", left - 7);
    label.setAttribute("y", y + 4);
    label.setAttribute("text-anchor", "end");
    label.classList.add("chart-axis");
    label.textContent = String(Math.round(mapped));
    svg.appendChild(label);
  }

  const axisLabel = (x, y, text, anchor) => {
    const node = document.createElementNS(namespace, "text");
    node.setAttribute("x", x);
    node.setAttribute("y", y);
    node.setAttribute("text-anchor", anchor);
    node.classList.add("chart-axis");
    node.textContent = text;
    svg.appendChild(node);
  };
  axisLabel(left, 154, "0°C", "start");
  axisLabel(left + width, 154, `${Math.round(maxTemp)}°C`, "end");

  if (!points.length) {
    const empty = document.createElementNS(namespace, "text");
    empty.setAttribute("x", viewWidth / 2);
    empty.setAttribute("y", 86);
    empty.setAttribute("text-anchor", "middle");
    empty.classList.add("empty-chart");
    empty.textContent = options.emptyText || "Empty curve · NVIDIA automatic";
    svg.appendChild(empty);
    return;
  }

  const xAt = (temperature) => left + (temperature / maxTemp) * width;
  const yAt = (fan) => {
    const span = Math.max(1, maxFan - minFan);
    const normalized = Math.max(0, Math.min(1, (fan - minFan) / span));
    return top + height - normalized * height;
  };

  axisLabel(xAt(points[0].temperature_celsius / 2), top + height / 2, "Auto", "middle");
  let pathData = `M ${xAt(points[0].temperature_celsius).toFixed(2)} ${yAt(minFan).toFixed(2)}`;
  let current = minFan;
  points.forEach((point) => {
    pathData += ` L ${xAt(point.temperature_celsius).toFixed(2)} ${yAt(current).toFixed(2)}`;
    current = point.fan_percent;
    pathData += ` L ${xAt(point.temperature_celsius).toFixed(2)} ${yAt(current).toFixed(2)}`;
  });
  pathData += ` L ${xAt(maxTemp).toFixed(2)} ${yAt(current).toFixed(2)}`;

  const descending = document.createElementNS(namespace, "path");
  let downData = "";
  let previous = minFan;
  points.forEach((point) => {
    const x = xAt(Math.max(0, point.temperature_celsius - 3));
    downData += ` M ${x} ${yAt(previous)} L ${x} ${yAt(point.fan_percent)}`;
    previous = point.fan_percent;
  });
  descending.setAttribute("d", downData);
  descending.setAttribute("stroke", COLORS.slate);
  descending.dataset.series = "cool";
  descending.setAttribute("stroke-dasharray", "4 4");
  descending.classList.add("chart-line");
  svg.appendChild(descending);
  const path = document.createElementNS(namespace, "path");
  path.setAttribute("d", pathData);
  path.setAttribute("stroke", COLORS.blue);
  path.dataset.series = "rise";
  path.classList.add("chart-line");
  svg.appendChild(path);

  points.forEach((point) => {
    const circle = document.createElementNS(namespace, "circle");
    circle.setAttribute("cx", xAt(point.temperature_celsius));
    circle.setAttribute("cy", yAt(point.fan_percent));
    circle.setAttribute("r", "3.2");
    circle.setAttribute("fill", COLORS.blue);
    circle.dataset.series = "rise";
    circle.setAttribute("stroke", "var(--card-surface)");
    circle.setAttribute("stroke-width", "1.5");
    svg.appendChild(circle);
  });
  const block = svg.closest(".fan-curve-chart");
  if (block) window.highlightChartSeries(block, block.dataset.activeSeries || null);
}

function createCurveChartBlock(title) {
  const block = textElement("div", "chart-block fan-curve-chart", "");
  block.appendChild(textElement("div", "chart-title", title));
  const legend = textElement("div", "chart-legend", "");
  for (const [key, label, color] of [["rise", "Rise", COLORS.blue], ["cool", "Cool down", COLORS.slate]]) {
    const item = textElement("button", "legend-item", "");
    item.type = "button";
    item.dataset.series = key;
    const swatch = textElement("i", key === "cool" ? "dashed-swatch" : "", "");
    swatch.style.background = color;
    item.append(swatch, document.createTextNode(label));
    item.addEventListener("click", () => {
      item.blur();
      block.dataset.activeSeries = block.dataset.activeSeries === key ? "" : key;
      window.highlightChartSeries(block, block.dataset.activeSeries || null);
    });
    legend.appendChild(item);
  }
  const item = textElement("span", "curve-step-count", "");
  legend.appendChild(item);
  const chart = textElement("div", "chart", "");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", title);
  chart.appendChild(svg);
  block.append(legend, chart);
  const highlight = (key) => {
    block.dataset.activeSeries = key || "";
    window.highlightChartSeries(block, key);
  };
  block.addEventListener("pointerover", (event) => highlight(event.target.closest("[data-series]")?.dataset.series || null));
  block.addEventListener("pointerleave", () => highlight(null));
  block.addEventListener("focusin", (event) => highlight(event.target.dataset.series || null));
  block.addEventListener("focusout", () => highlight(null));
  svg.resizeObserver = new ResizeObserver(() => {
    const width = Math.max(240, chart.clientWidth || 640);
    if (svg.curveProfile && width !== svg.viewBox.baseVal.width) {
      drawFanCurve(svg, svg.curveProfile, svg.curveOptions);
    }
  });
  svg.resizeObserver.observe(chart);
  return { block, svg, legendItem: item };
}

function updateCurveLegend(legendItem, profile) {
  const count = (profile.curve_points || []).length;
  legendItem.textContent = count ? `${count} step${count === 1 ? "" : "s"}` : "Automatic";
}

function createFanControl(gpu) {
  const control = textElement("div", "fan-control", "");
  const header = textElement("div", "fan-control-header", "");
  const visual = textElement("div", "fan-visual", "");
  const icon = createFanIcon();
  const actual = textElement("strong", "", "N/A");
  visual.append(icon, actual);

  const summary = textElement("div", "fan-summary", "");
  const modeBadge = textElement("span", "fan-mode-badge", "Automatic");
  const editButton = document.createElement("button");
  editButton.type = "button";
  editButton.className = "small-button";
  editButton.textContent = "Edit curve";
  summary.append(modeBadge, editButton);
  header.append(visual, summary);

  const chart = createCurveChartBlock("Fan response curve");
  const message = textElement("p", "fan-control-message", "");
  control.append(header, chart.block, message);

  const state = gpu.fan_control || {};
  const nodes = {
    modeBadge, message, icon, actual, editButton,
    preview: chart.svg, legendItem: chart.legendItem,
    pending: false, latest: gpu,
    profile: profileFromState(state),
  };
  fanControls.set(gpu.uuid, nodes);
  editButton.addEventListener("click", () => openFanEditor(gpu.uuid));
  drawFanCurve(nodes.preview, nodes.profile);
  updateCurveLegend(nodes.legendItem, nodes.profile);
  return control;
}

function updateFanControl(gpu, overrideMessage = null) {
  const nodes = fanControls.get(gpu.uuid);
  if (!nodes) return;
  nodes.latest = gpu;
  const state = gpu.fan_control || {};
  const supported = Boolean(state.supported);
  nodes.profile = profileFromState(state);
  drawFanCurve(nodes.preview, nodes.profile);
  updateCurveLegend(nodes.legendItem, nodes.profile);

  const mode = state.applied_percent != null ? "Step control" : "Automatic";
  const applied = state.applied_percent == null ? "" : ` · ${state.applied_percent}%`;
  nodes.modeBadge.textContent = `${mode}${applied}`;
  nodes.editButton.disabled = nodes.pending || !supported;
  if (!supported) {
    nodes.editButton.title = state.error || "Fan control unavailable";
  } else {
    nodes.editButton.removeAttribute("title");
  }

  const speed = Number(gpu.fan_percent);
  nodes.actual.textContent = formatPercent(gpu.fan_percent);
  nodes.icon.style.setProperty("--fan-color", fanColor(gpu.temperature_celsius));
  if (Number.isFinite(speed) && speed > 0) {
    nodes.icon.style.setProperty("--fan-duration", `${Math.max(.3, 4 - speed * .035)}s`);
    nodes.icon.classList.add("spinning");
  } else {
    nodes.icon.classList.remove("spinning");
  }
  nodes.icon.setAttribute(
    "aria-label",
    `GPU fan ${formatPercent(gpu.fan_percent)}, temperature ${formatTemperature(gpu.temperature_celsius)}`,
  );

  if (overrideMessage) {
    nodes.message.textContent = overrideMessage;
    nodes.message.className = "fan-control-message error";
  } else if (!supported) {
    nodes.message.textContent = state.error || "Fan control is unavailable.";
    nodes.message.className = "fan-control-message error";
  } else if (state.error) {
    nodes.message.textContent = state.error;
    nodes.message.className = "fan-control-message error";
  } else if (!(state.curve_points || []).length) {
    nodes.message.textContent = "Empty curve uses NVIDIA automatic fan control.";
    nodes.message.className = "fan-control-message";
  } else {
    nodes.message.textContent = state.cooldown_remaining_seconds != null
      ? `Cooling down · next lower step in ${Math.ceil(state.cooldown_remaining_seconds)}s`
      : `Rise at each threshold · drop after ${state.cooldown_seconds ?? 20}s at ${state.hysteresis_celsius ?? 3}°C below it`;
    nodes.message.className = "fan-control-message";
  }

  if (openFanEditorUuid === gpu.uuid) {
    const dialog = $("#fan-editor-dialog");
    if (dialog && !dialog.dataset.dirty) {
      dialog.dataset.revision = String(state.revision ?? 0);
    }
  }
}

function closeFanEditor() {
  openFanEditorUuid = null;
  $("#modal-root").querySelectorAll("svg").forEach((svg) => svg.resizeObserver?.disconnect());
  $("#modal-root").replaceChildren();
  document.body.style.overflow = "";
}

function openFanEditor(gpuUuid) {
  const nodes = fanControls.get(gpuUuid);
  if (!nodes || nodes.pending) return;
  const gpu = nodes.latest;
  const state = gpu.fan_control || {};
  if (!state.supported) return;

  openFanEditorUuid = gpuUuid;
  const draft = profileFromState(state);
  const root = $("#modal-root");
  root.querySelectorAll("svg").forEach((svg) => svg.resizeObserver?.disconnect());
  root.replaceChildren();
  document.body.style.overflow = "hidden";

  const backdrop = textElement("div", "modal-backdrop", "");
  backdrop.setAttribute("role", "presentation");
  const dialog = textElement("div", "modal-dialog", "");
  dialog.id = "fan-editor-dialog";
  dialog.dataset.dirty = "";
  dialog.dataset.revision = String(draft.revision);
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", "fan-editor-title");

  const header = textElement("div", "modal-header", "");
  const heading = textElement("div", "", "");
  heading.append(
    textElement("h3", "", "Edit fan curve"),
    textElement(
      "p",
      "modal-subtitle",
      `GPU ${gpu.index} · ${gpu.name || gpu.uuid}`,
    ),
  );
  heading.querySelector("h3").id = "fan-editor-title";
  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "modal-close";
  closeButton.setAttribute("aria-label", "Close without saving");
  closeButton.textContent = "×";
  heading.appendChild(textElement("p", "modal-subtitle", "Rise immediately at a threshold. Stay 3°C below it for the hold time to drop one step. Below the first step, return to automatic."));
  header.append(heading, closeButton);

  const limits = textElement("div", "fan-limits", "");
  const fields = [
    ["minimum_percent", "Min fan %"],
    ["maximum_percent", "Max fan %"],
    ["cooldown_seconds", "Cool-down hold (seconds)"],
  ].map(([key, labelText]) => {
    const label = textElement("label", "", labelText);
    const input = document.createElement("input");
    input.type = "number";
    input.dataset.field = key;
    input.value = String(draft[key]);
    label.appendChild(input);
    limits.appendChild(label);
    return input;
  });

  const chart = createCurveChartBlock("Preview");
  const points = textElement("div", "fan-curve-points", "");
  const actions = textElement("div", "modal-actions", "");
  const leftActions = textElement("div", "modal-actions-left", "");
  const rightActions = textElement("div", "modal-actions-right", "");
  const addButton = document.createElement("button");
  addButton.type = "button";
  addButton.className = "small-button";
  addButton.textContent = "Add point";
  const discardButton = document.createElement("button");
  discardButton.type = "button";
  discardButton.className = "small-button";
  discardButton.textContent = "Don't save";
  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.className = "small-button primary";
  saveButton.textContent = "Save";
  leftActions.appendChild(addButton);
  rightActions.append(discardButton, saveButton);
  actions.append(leftActions, rightActions);
  const message = textElement("p", "fan-control-message", "");

  dialog.append(header, limits, chart.block, points, actions, message);
  backdrop.appendChild(dialog);
  root.appendChild(backdrop);

  const editor = { draft, fields, points, preview: chart.svg, legendItem: chart.legendItem, message, saveButton };

  const markDirty = () => {
    dialog.dataset.dirty = "1";
  };

  const refreshPreview = () => {
    drawFanCurve(editor.preview, editor.draft);
    updateCurveLegend(editor.legendItem, editor.draft);
  };

  const renderPoints = () => {
    editor.draft.curve_points = sortedPoints(editor.draft.curve_points);
    editor.points.replaceChildren();
    if (!editor.draft.curve_points.length) {
      editor.points.appendChild(
        textElement("p", "fan-point-empty", "No points — saving will use NVIDIA automatic mode."),
      );
      refreshPreview();
      return;
    }
    editor.draft.curve_points.forEach((point, index) => {
      const row = textElement("div", "fan-point-row", "");
      const tempLabel = textElement("label", "", "Temperature °C");
      const tempInput = document.createElement("input");
      tempInput.type = "number";
      tempInput.min = "0";
      tempInput.max = "120";
      tempInput.step = "1";
      tempInput.value = String(point.temperature_celsius);
      tempLabel.appendChild(tempInput);
      const fanLabel = textElement("label", "", "Fan %");
      const fanInput = document.createElement("input");
      fanInput.type = "number";
      fanInput.min = String(editor.draft.minimum_percent);
      fanInput.max = String(editor.draft.maximum_percent);
      fanInput.step = String(editor.draft.step_percent || 5);
      fanInput.value = String(point.fan_percent);
      fanLabel.appendChild(fanInput);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "small-button danger-quiet";
      remove.textContent = "Remove";
      const readDomPoints = () => [...editor.points.querySelectorAll(".fan-point-row")].map((item) => {
        const inputs = item.querySelectorAll("input");
        return {
          temperature_celsius: Number(inputs[0].value),
          fan_percent: Number(inputs[1].value),
        };
      });
      const syncDraft = (resort = false) => {
        markDirty();
        editor.draft.curve_points = readDomPoints();
        if (resort) renderPoints();
        else refreshPreview();
      };
      tempInput.addEventListener("change", () => syncDraft(true));
      tempInput.addEventListener("input", () => syncDraft(false));
      fanInput.addEventListener("input", () => syncDraft(false));
      remove.addEventListener("click", () => {
        markDirty();
        editor.draft.curve_points = readDomPoints().filter((_, itemIndex) => itemIndex !== index);
        renderPoints();
      });
      row.append(tempLabel, fanLabel, remove);
      editor.points.appendChild(row);
    });
    refreshPreview();
  };

  fields.forEach((input) => {
    input.addEventListener("input", () => {
      markDirty();
      editor.draft[input.dataset.field] = Number(input.value);
      renderPoints();
    });
  });
  addButton.addEventListener("click", () => {
    markDirty();
    const existing = sortedPoints(editor.draft.curve_points);
    const last = existing[existing.length - 1];
    const nextTemp = last ? last.temperature_celsius + 5 : 80;
    const nextFan = last
      ? Math.min(editor.draft.maximum_percent, last.fan_percent + (editor.draft.step_percent || 5))
      : 80;
    editor.draft.curve_points.push({
      temperature_celsius: nextTemp,
      fan_percent: nextFan,
    });
    renderPoints();
  });

  const cleanupAndClose = () => {
    document.removeEventListener("keydown", onKeydown);
    closeFanEditor();
  };
  const onKeydown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      cleanupAndClose();
    }
  };
  document.addEventListener("keydown", onKeydown);
  closeButton.addEventListener("click", cleanupAndClose);
  discardButton.addEventListener("click", cleanupAndClose);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) cleanupAndClose();
  });
  saveButton.addEventListener("click", async () => {
    saveButton.disabled = true;
    addButton.disabled = true;
    discardButton.disabled = true;
    editor.message.textContent = "Saving…";
    editor.message.className = "fan-control-message";
    try {
      await submitFanProfile(gpuUuid, {
        ...editor.draft,
        curve_points: sortedPoints(editor.draft.curve_points),
        expected_revision: Number(dialog.dataset.revision || draft.revision),
      });
      cleanupAndClose();
    } catch (error) {
      saveButton.disabled = false;
      addButton.disabled = false;
      discardButton.disabled = false;
      if (error.detail?.fan_control) {
        dialog.dataset.revision = String(error.detail.fan_control.revision ?? dialog.dataset.revision);
      }
      editor.message.textContent = error.status === 409
        ? `Not saved: ${error.message}. Update from the latest revision and try again.`
        : `Unable to save: ${error.message}`;
      editor.message.className = "fan-control-message error";
    }
  });

  renderPoints();
  fields[0]?.focus();
}

async function submitFanProfile(gpuUuid, payload) {
  const nodes = fanControls.get(gpuUuid);
  const control = nodes?.latest?.fan_control;
  if (!nodes || !control) throw new Error("GPU fan control unavailable");
  nodes.pending = true;
  updateFanControl(nodes.latest);
  try {
    const updated = await request(`/api/gpus/${encodeURIComponent(gpuUuid)}/fan-profile`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        minimum_percent: Number(payload.minimum_percent),
        maximum_percent: Number(payload.maximum_percent),
        cooldown_seconds: Number(payload.cooldown_seconds),
        curve_points: payload.curve_points,
        expected_revision: payload.expected_revision,
      }),
    });
    nodes.latest.fan_control = updated;
    nodes.pending = false;
    updateFanControl(nodes.latest);
    return updated;
  } catch (error) {
    nodes.pending = false;
    if (error.detail?.fan_control) {
      nodes.latest.fan_control = error.detail.fan_control;
    }
    updateFanControl(nodes.latest);
    throw error;
  }
}

function yesNo(value) {
  const span = textElement(
    "span",
    `user-settings-flag ${value ? "on" : "off"}`,
    value ? "Yes" : "No",
  );
  return span;
}

function setUserSettingsEditMode(editing) {
  userSettingsEditing = editing;
  const button = $("#user-settings-edit");
  button.textContent = editing ? "Done" : "Edit";
  button.setAttribute("aria-pressed", String(editing));
  if (latestUserSettings) renderUserSettings(latestUserSettings);
}

function renderUserSettings(payload) {
  latestUserSettings = payload;
  const body = $("#user-settings tbody");
  const users = payload?.users || [];
  const sync = payload?.last_sync;
  const meta = $("#user-settings-sync-meta");
  const actionsCol = $("#user-settings .user-settings-actions-col");
  if (sync?.synced_at) {
    meta.textContent = (
      `Last sync ${new Date(sync.synced_at * 1000).toLocaleString()} · `
      + `${sync.active ?? users.length} active`
    );
  } else {
    meta.textContent = "Synced Linux login users";
  }
  actionsCol.hidden = !userSettingsEditing;
  document.getElementById("user-settings").classList.toggle(
    "is-editing",
    userSettingsEditing,
  );
  body.replaceChildren();
  if (!users.length) {
    const row = document.createElement("tr");
    const cell = textElement("td", "empty", "No login users synced yet");
    cell.colSpan = userSettingsEditing ? 6 : 5;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  users.forEach((user) => {
    const row = document.createElement("tr");
    row.appendChild(textElement("td", "", user.username));

    if (!userSettingsEditing) {
      const emailCell = document.createElement("td");
      emailCell.appendChild(
        user.email
          ? textElement("span", "user-settings-email-text", user.email)
          : textElement("span", "user-settings-email-text muted", "Not set"),
      );
      const adminCell = document.createElement("td");
      adminCell.appendChild(yesNo(user.is_admin));
      const tempCell = document.createElement("td");
      tempCell.appendChild(yesNo(user.notify_temperature));
      const processCell = document.createElement("td");
      processCell.appendChild(yesNo(user.notify_process_end));
      row.append(emailCell, adminCell, tempCell, processCell);
      body.appendChild(row);
      return;
    }

    const emailCell = document.createElement("td");
    const email = document.createElement("input");
    email.className = "user-settings-email";
    email.type = "email";
    email.setAttribute("aria-label", `Email address for ${user.username}`);
    email.value = user.email || "";
    email.placeholder = "name@example.com";
    emailCell.appendChild(email);
    row.appendChild(emailCell);

    const makeCheck = (checked, label, setting) => {
      const cell = document.createElement("td");
      const wrap = textElement("label", "user-settings-check", "");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = Boolean(checked);
      input.setAttribute("aria-label", `${setting} for ${user.username}`);
      wrap.append(input, document.createTextNode(label));
      cell.appendChild(wrap);
      return { cell, input };
    };
    const admin = makeCheck(user.is_admin, "Admin", "Administrator");
    const temp = makeCheck(user.notify_temperature, "On", "Temperature alerts");
    const processEnd = makeCheck(user.notify_process_end, "On", "Process-end notifications");
    row.append(admin.cell, temp.cell, processEnd.cell);

    const action = document.createElement("td");
    const save = document.createElement("button");
    save.type = "button";
    save.className = "small-button primary";
    save.textContent = "Save";
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await request(`/api/settings/users/${encodeURIComponent(user.username)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: email.value.trim() || null,
            is_admin: admin.input.checked,
            notify_temperature: temp.input.checked,
            notify_process_end: processEnd.input.checked,
          }),
        });
        await refreshUserSettings();
      } catch (error) {
        window.alert(`Unable to save ${user.username}: ${error.message}`);
        save.disabled = false;
      }
    });
    action.appendChild(save);
    row.appendChild(action);
    body.appendChild(row);
  });
}

async function refreshUserSettings() {
  const payload = await request("/api/settings/users");
  renderUserSettings(payload);
}

function setupSystemCards() {
  if (charts.has("system-cpu:history")) return;
  $("#system-summary").replaceChildren(
    createMetricCard("system-cpu", "CPU", "Aggregate processor activity", [
      { id: "usage", primary: true },
      { id: "temperature", label: "Maximum temperature" },
      { id: "cores", label: "Logical cores" },
      { id: "load", label: "Load 1 / 5 / 15" },
      { id: "uptime", label: "Uptime" },
    ], [{
      id: "history",
      title: "Usage and temperature",
      series: [
        series("cpu_usage_percent", "Usage", COLORS.blue),
        series("cpu_temperature_celsius", "Temperature", COLORS.coral, formatTemperature),
      ],
    }]),
    createMetricCard("system-memory", "Memory", "Physical memory pressure", [
      { id: "usage", primary: true },
      { id: "used", label: "Used" },
      { id: "available", label: "Available" },
    ], [{
      id: "history",
      title: "Usage",
      series: [series("memory_usage_percent", "Memory", COLORS.teal)],
    }]),
    createMetricCard("system-swap", "Swap", "Overflow memory activity", [
      { id: "usage", primary: true },
      { id: "used", label: "Used" },
      { id: "sampled", label: "Last sample" },
    ], [{
      id: "history",
      title: "Usage",
      series: [series("swap_usage_percent", "Swap", COLORS.violet)],
    }]),
  );
}

function renderSystem(data) {
  setupSystemCards();
  const cpu = data.cpu || {};
  const temperature = data.cpu_temperature || {};
  const memory = data.memory || {};
  const swap = data.swap || {};
  const load = cpu.load_average || {};
  setMetric("system-cpu", "usage", formatPercent(cpu.usage_percent));
  setMetric("system-cpu", "temperature", formatTemperature(temperature.max_celsius));
  setMetric("system-cpu", "cores", cpu.logical_count ?? "N/A");
  setMetric("system-cpu", "load", [load["1m"], load["5m"], load["15m"]]
    .map((value) => value == null ? "N/A" : Number(value).toFixed(2)).join(" / "));
  setMetric("system-cpu", "uptime", cpu.boot_time
    ? formatDuration(Date.now() / 1000 - cpu.boot_time) : "N/A");
  setMetric("system-memory", "usage", formatPercent(memory.usage_percent));
  setMetric("system-memory", "used",
    `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)}`);
  setMetric("system-memory", "available", formatBytes(memory.available_bytes));
  setMetric("system-swap", "usage", formatPercent(swap.usage_percent));
  setMetric("system-swap", "used",
    `${formatBytes(swap.used_bytes)} / ${formatBytes(swap.total_bytes)}`);
  setMetric("system-swap", "sampled", data.sampled_at
    ? new Date(data.sampled_at * 1000).toLocaleTimeString() : "Waiting");
}

function renderGpus(gpus) {
  const signature = gpus.map((gpu) => gpu.uuid).sort().join("|");
  if (signature !== gpuSignature) {
    clearCards("gpu");
    const cards = gpus.map((gpu) => {
      const card = createMetricCard(
        `gpu:${gpu.uuid}`,
        `GPU ${gpu.index}`,
        gpu.name,
        [
          { id: "utilization", primary: true, highlight: ["load", "utilization_percent"] },
          { id: "temperature", label: "Temperature", highlight: ["thermal", "temperature_celsius"] },
          { id: "memory", label: "Memory", highlight: ["load", "memory_used_bytes"] },
          { id: "fan", label: "Fan" },
          { id: "power", label: "Power", highlight: ["thermal", "power_watts"] },
          { id: "workloads", label: "Workloads" },
        ],
        [
          {
            id: "load",
            title: "Compute and memory load",
            series: [
              series("utilization_percent", "GPU", COLORS.blue),
              series("memory_used_bytes", "VRAM", COLORS.teal, formatPercent,
                (point) => point.memory_total_bytes
                  ? point.memory_used_bytes / point.memory_total_bytes * 100 : null),
            ],
          },
          {
            id: "thermal",
            title: "Thermals and power",
            series: [
              series("temperature_celsius", "Temperature", COLORS.coral, formatTemperature),
              series("power_watts", "Power", COLORS.violet, formatPercent,
                (point) => point.power_limit_watts
                  ? point.power_watts / point.power_limit_watts * 100 : null),
            ],
          },
        ],
      );
      card.classList.add("gpu-card");
      card.appendChild(createFanControl(gpu));
      return card;
    });
    $("#gpus").replaceChildren(...cards);
    if (!cards.length) $("#gpus").appendChild(
      textElement("p", "empty panel-empty", "No NVIDIA GPU data available"),
    );
    gpuSignature = signature;
  }
  gpus.forEach((gpu) => {
    const key = `gpu:${gpu.uuid}`;
    setMetric(key, "utilization", formatPercent(gpu.utilization_percent));
    setMetric(key, "temperature", formatTemperature(gpu.temperature_celsius));
    setMetric(key, "memory",
      `${formatBytes(gpu.memory_used_bytes)} / ${formatBytes(gpu.memory_total_bytes)}`);
    setMetric(key, "fan", formatPercent(gpu.fan_percent));
    setMetric(key, "power", gpu.power_watts == null ? "N/A"
      : `${gpu.power_watts.toFixed(1)} / ${gpu.power_limit_watts?.toFixed(1) ?? "N/A"} W`);
    const workloads = metricNodes.get(`${key}:workloads`);
    workloads.tabIndex = 0;
    workloads.setAttribute("aria-label", "GPU workloads and users; scroll to see all users");
    const users = gpu.users || [];
    workloads.replaceChildren(
      textElement("span", "workload-count", `${gpu.process_count ?? 0} processes`),
      ...users.map((user) => textElement("span", "user-chip", user)),
      ...(users.length ? [] : [textElement("span", "workload-idle", "Available")]),
    );
    updateFanControl(gpu);
  });
}

function renderDisks(disks) {
  latestDisks = disks;
  if (diskView === "physical") {
    clearCards("disk");
    const groups = new Map();
    disks.forEach((disk) => {
      const physicalDisks = disk.physical_disks?.length
        ? disk.physical_disks
        : [{ name: disk.device, device: disk.device }];
      physicalDisks.forEach((physical) => {
        const key = physical.device || physical.name || disk.device;
        const group = groups.get(key) || { ...physical, mounts: [] };
        group.mounts.push(disk);
        groups.set(key, group);
      });
    });
    const cards = [...groups.values()].map((group) => {
      const card = textElement("article", "card physical-disk-card", "");
      const heading = textElement("div", "card-heading", "");
      const headingText = textElement("div", "", "");
      headingText.append(
        textElement("h3", "", group.device || group.name),
        textElement("p", "card-subtitle", group.model || "Physical disk"),
      );
      heading.appendChild(headingText);
      card.appendChild(heading);

      const uniqueFilesystems = new Map();
      group.mounts.forEach((disk) => uniqueFilesystems.set(disk.device, disk));
      const filesystems = [...uniqueFilesystems.values()];
      const total = filesystems.reduce((sum, disk) => sum + Number(disk.total_bytes || 0), 0);
      const used = filesystems.reduce((sum, disk) => sum + Number(disk.used_bytes || 0), 0);
      const usage = total ? used / total * 100 : 0;

      const overview = textElement("div", "physical-disk-overview", "");
      const pie = textElement("div", "disk-pie", "");
      pie.style.setProperty("--disk-usage", `${Math.min(100, usage) * 3.6}deg`);
      pie.setAttribute("role", "img");
      pie.setAttribute("aria-label", `${usage.toFixed(1)}% of mounted space used`);
      pie.appendChild(textElement("strong", "", formatPercent(usage)));
      const details = textElement("div", "physical-disk-details", "");
      details.append(
        textElement("span", "label", "Mounted space"),
        textElement("strong", "", `${formatBytes(used)} / ${formatBytes(total)}`),
        textElement("span", "label", "Drive capacity"),
        textElement("strong", "", formatBytes(group.size_bytes)),
        textElement("span", "label", "Type"),
        textElement("strong", "", group.rotational == null
          ? "N/A" : (group.rotational ? "HDD" : "SSD")),
      );
      overview.append(pie, details);
      card.appendChild(overview);

      const mounts = textElement("div", "physical-mounts", "");
      mounts.appendChild(textElement("span", "chart-title", "Mounted filesystems"));
      group.mounts.forEach((disk) => {
        const row = textElement("div", "physical-mount", "");
        row.append(
          textElement("span", "", `${disk.mountpoint} · ${disk.filesystem}`),
          textElement("strong", "", formatPercent(disk.usage_percent)),
        );
        mounts.appendChild(row);
      });
      card.appendChild(mounts);
      return card;
    });
    $("#disks").replaceChildren(...cards);
    if (!cards.length) $("#disks").appendChild(
      textElement("p", "empty panel-empty", "No physical disk data available"),
    );
    diskSignature = "physical";
    return;
  }

  const signature = disks.map((disk) => disk.mountpoint).sort().join("|");
  if (`mounts:${signature}` !== diskSignature) {
    clearCards("disk");
    const cards = disks.map((disk) => createMetricCard(
      `disk:${disk.mountpoint}`,
      disk.mountpoint,
      `${disk.device} · ${disk.filesystem}`,
      [
        { id: "usage", primary: true },
        { id: "used", label: "Used" },
        { id: "available", label: "Available" },
        { id: "total", label: "Capacity" },
      ],
      [{
        id: "history",
        title: "Space used",
        series: [series("usage_percent", "Usage", COLORS.teal)],
      }],
    ));
    $("#disks").replaceChildren(...cards);
    if (!cards.length) $("#disks").appendChild(
      textElement("p", "empty panel-empty", "No physical disk data available"),
    );
    diskSignature = `mounts:${signature}`;
  }
  disks.forEach((disk) => {
    const key = `disk:${disk.mountpoint}`;
    setMetric(key, "usage", formatPercent(disk.usage_percent));
    setMetric(key, "used", formatBytes(disk.used_bytes));
    setMetric(key, "available", formatBytes(disk.available_bytes));
    setMetric(key, "total", formatBytes(disk.total_bytes));
  });
}

function renderUsers(users) {
  latestUsers = users;
  const systemUsers = users.filter((user) => user.is_primary === false);
  const visibleUsers = showSystemUsers
    ? users
    : users.filter((user) => user.is_primary !== false);
  replaceRows("#users", visibleUsers.map((user) => [
    user.username, user.process_count, formatPercent(user.cpu_percent),
    formatBytes(user.memory_rss_bytes), user.gpu_process_count, formatBytes(user.gpu_memory_bytes),
  ]));
  const button = $("#users-toggle");
  button.hidden = !systemUsers.length;
  button.textContent = showSystemUsers
    ? "Hide system users"
    : `Show system users (${systemUsers.length})`;
  button.setAttribute("aria-pressed", String(showSystemUsers));
}

function replaceRows(selector, rows) {
  const body = $(`${selector} tbody`);
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = textElement("td", "empty", "No data available");
    cell.colSpan = $(`${selector} thead tr`).children.length;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  rows.forEach((values) => {
    const row = document.createElement("tr");
    values.forEach((value) => {
      const cell = textElement("td", "", "");
      if (value instanceof Node) cell.appendChild(value);
      else cell.textContent = String(value);
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
}

function updateHistoryCharts() {
  if (!historyData) return;
  const system = historyData.system || [];
  charts.get("system-cpu:history")?.update(system);
  charts.get("system-memory:history")?.update(system);
  charts.get("system-swap:history")?.update(system);
  (historyData.gpus || []).forEach((gpu) => {
    charts.get(`gpu:${gpu.uuid}:load`)?.update(gpu.points || []);
    charts.get(`gpu:${gpu.uuid}:thermal`)?.update(gpu.points || []);
  });
  (historyData.disks || []).forEach((disk) => {
    charts.get(`disk:${disk.mountpoint}:history`)?.update(disk.points || []);
  });
}

async function request(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(
      typeof data?.detail === "string"
        ? data.detail
        : (data?.detail?.message || `${path}: HTTP ${response.status}`),
    );
    error.status = response.status;
    error.detail = data?.detail;
    throw error;
  }
  return data;
}

function openProcessCommand(process) {
  const dialog = $("#command-dialog");
  $("#command-context").textContent = `GPU ${process.gpu_index} · ${process.username} · PID ${process.pid}`;
  $("#command-full").textContent = process.command || process.executable || "N/A";
  $("#command-copy-status").textContent = "";
  dialog.showModal();
}

function processCommand(process) {
  const button = textElement("button", "command-open", "");
  button.type = "button";
  button.append(textElement("span", "command-preview", ""), textElement("span", "command-open-label", "View ↗"));
  button.addEventListener("click", () => openProcessCommand(button.process));
  updateProcessCommand(button, process);
  return button;
}

function updateProcessCommand(button, process) {
  button.process = process;
  button.setAttribute("aria-label", `View command for PID ${process.pid}`);
  button.setAttribute("aria-haspopup", "dialog");
  button.querySelector(".command-preview").textContent = process.command || process.executable || "N/A";
}

function renderGpuProcesses(processes) {
  if (!processes.length) {
    replaceRows("#gpu-processes", []);
    return;
  }
  const body = $("#gpu-processes tbody");
  const existing = new Map([...body.rows].map((row) => [row.dataset.key, row]));
  const active = new Set();
  processes.forEach((process, index) => {
    const key = `${process.gpu_index}:${process.pid}:${process.create_time ?? ""}`;
    let row = existing.get(key);
    if (!row) {
      row = document.createElement("tr");
      row.dataset.key = key;
      for (let i = 0; i < 8; i++) row.appendChild(document.createElement("td"));
      row.cells[7].appendChild(processCommand(process));
    }
    const values = [process.gpu_index, process.username, process.pid,
      formatBytes(process.gpu_memory_bytes), formatPercent(process.cpu_percent),
      formatBytes(process.memory_rss_bytes), formatDuration(process.runtime_seconds)];
    values.forEach((value, i) => {
      if (row.cells[i].textContent !== String(value)) row.cells[i].textContent = String(value);
    });
    updateProcessCommand(row.cells[7].firstElementChild, process);
    // Leave unchanged rows in place so refreshes preserve focus and selection.
    if (body.children[index] !== row) body.insertBefore(row, body.children[index] || null);
    active.add(row);
  });
  [...body.children].forEach((row) => { if (!active.has(row)) row.remove(); });
}

$("#command-copy").addEventListener("click", async () => {
  const command = $("#command-full");
  try {
    await navigator.clipboard.writeText(command.textContent);
    $("#command-copy-status").textContent = "Copied to clipboard";
  } catch {
    const range = document.createRange();
    range.selectNodeContents(command);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    $("#command-copy-status").textContent = "Command selected — press Ctrl+C or ⌘C to copy";
  }
});
$("#command-dialog").addEventListener("click", (event) => {
  const dialog = event.currentTarget;
  const bounds = dialog.getBoundingClientRect();
  if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right
      || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
});

async function refreshLive() {
  const [summary, gpus, processes, users, disks, alerts] = await Promise.all([
    request("/api/summary"), request("/api/gpus"), request("/api/gpu-processes"),
    request("/api/users"), request("/api/disks"), request("/api/alerts"),
  ]);
  renderSystem(summary);
  renderGpus(gpus);
  renderDisks(disks);
  renderGpuProcesses(processes);
  renderUsers(users);
  replaceRows("#alerts", alerts.map((alert) => [
    alert.gpu_index, new Date(alert.triggered_at * 1000).toLocaleString(), alert.status,
    formatTemperature(alert.current_temperature), formatTemperature(alert.max_temperature),
    (alert.users || []).join(", ") || "None", alert.email_status || "not attempted",
  ]));
  updateHistoryCharts();
  $("#connection").textContent = "Live";
  $("#connection").className = "status live";
}

async function updateLive() {
  try {
    await refreshLive();
  } catch (error) {
    $("#connection").textContent = `Unavailable: ${error.message}`;
    $("#connection").className = "status error";
  }
}

async function refreshHistory() {
  try {
    const range = Number($("#history-range").value);
    historyData = await request(`/api/history?range_seconds=${range}&max_points=720`);
    updateHistoryCharts();
  } catch (error) {
    console.error("Unable to refresh metric history", error);
  }
}

$("#history-range").addEventListener("change", refreshHistory);
$("#disk-view-toggle").addEventListener("click", () => {
  diskView = diskView === "mounts" ? "physical" : "mounts";
  $("#disk-view-toggle").textContent = diskView === "mounts"
    ? "Group by physical disk"
    : "Show mount points";
  $("#disk-view-toggle").setAttribute("aria-pressed", String(diskView === "physical"));
  renderDisks(latestDisks);
  updateHistoryCharts();
});
$("#users-toggle").addEventListener("click", () => {
  showSystemUsers = !showSystemUsers;
  renderUsers(latestUsers);
});
$("#user-settings-sync").addEventListener("click", async () => {
  const button = $("#user-settings-sync");
  button.disabled = true;
  try {
    const payload = await request("/api/settings/users/sync", { method: "POST" });
    renderUserSettings(payload);
  } catch (error) {
    window.alert(`Unable to sync users: ${error.message}`);
  } finally {
    button.disabled = false;
  }
});
$("#user-settings-edit").addEventListener("click", () => {
  setUserSettingsEditMode(!userSettingsEditing);
});
updateLive();
refreshHistory();
refreshUserSettings().catch((error) => {
  console.error("Unable to load user settings", error);
});
setInterval(updateLive, 2000);
setInterval(refreshHistory, 30000);
