"use strict";

const $ = (selector) => document.querySelector(selector);
const metricNodes = new Map();
const charts = new Map();
let gpuSignature = null;
let diskSignature = null;
let historyData = null;
let diskView = "mounts";
let latestDisks = [];
let latestUsers = [];
let showSystemUsers = false;

const COLORS = {
  cyan: "#22d3ee",
  violet: "#a78bfa",
  emerald: "#34d399",
  amber: "#fbbf24",
  rose: "#fb7185",
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
    const row = textElement("div", "metric", "");
    row.appendChild(textElement("span", "label", item.label));
    const value = textElement("strong", "", "N/A");
    metricNodes.set(`${key}:${item.id}`, value);
    row.appendChild(value);
    metrics.appendChild(row);
  });
  element.appendChild(metrics);

  chartDefinitions.forEach((definition) => {
    const block = textElement("div", "chart-block", "");
    if (definition.title) block.appendChild(textElement("span", "chart-title", definition.title));
    const holder = textElement("div", "chart", "");
    block.appendChild(holder);
    element.appendChild(block);
    charts.set(
      `${key}:${definition.id}`,
      new window.TimeSeriesChart(holder, {
        title: `${title} ${definition.title || "history"}`,
        series: definition.series,
      }),
    );
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
    if (key.startsWith(`${prefix}:`)) charts.delete(key);
  }
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
        series("cpu_usage_percent", "Usage", COLORS.cyan),
        series("cpu_temperature_celsius", "Temperature", COLORS.rose, formatTemperature),
      ],
    }]),
    createMetricCard("system-memory", "Memory", "Physical memory pressure", [
      { id: "usage", primary: true },
      { id: "used", label: "Used" },
      { id: "available", label: "Available" },
    ], [{
      id: "history",
      title: "Usage",
      series: [series("memory_usage_percent", "Memory", COLORS.violet)],
    }]),
    createMetricCard("system-swap", "Swap", "Overflow memory activity", [
      { id: "usage", primary: true },
      { id: "used", label: "Used" },
      { id: "sampled", label: "Last sample" },
    ], [{
      id: "history",
      title: "Usage",
      series: [series("swap_usage_percent", "Swap", COLORS.amber)],
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
    const cards = gpus.map((gpu) => createMetricCard(
      `gpu:${gpu.uuid}`,
      `GPU ${gpu.index}`,
      gpu.name,
      [
        { id: "utilization", primary: true },
        { id: "temperature", label: "Temperature" },
        { id: "memory", label: "Memory" },
        { id: "fan", label: "Fan" },
        { id: "power", label: "Power" },
        { id: "workloads", label: "Workloads" },
      ],
      [
        {
          id: "load",
          title: "Compute and memory load",
          series: [
            series("utilization_percent", "GPU", COLORS.cyan),
            series("memory_used_bytes", "VRAM", COLORS.violet, formatPercent,
              (point) => point.memory_total_bytes
                ? point.memory_used_bytes / point.memory_total_bytes * 100 : null),
          ],
        },
        {
          id: "thermal",
          title: "Thermals and power",
          series: [
            series("temperature_celsius", "Temperature", COLORS.rose, formatTemperature),
            series("power_watts", "Power", COLORS.amber, formatPercent,
              (point) => point.power_limit_watts
                ? point.power_watts / point.power_limit_watts * 100 : null),
          ],
        },
      ],
    ));
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
    setMetric(key, "workloads",
      `${gpu.process_count} process${gpu.process_count === 1 ? "" : "es"} · `
      + ((gpu.users || []).join(", ") || "No users"));
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
        series: [series("usage_percent", "Usage", COLORS.emerald)],
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
    values.forEach((value) => row.appendChild(textElement("td", "", String(value))));
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

async function request(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function refreshLive() {
  const [summary, gpus, processes, users, disks, alerts] = await Promise.all([
    request("/api/summary"), request("/api/gpus"), request("/api/gpu-processes"),
    request("/api/users"), request("/api/disks"), request("/api/alerts"),
  ]);
  renderSystem(summary);
  renderGpus(gpus);
  renderDisks(disks);
  replaceRows("#gpu-processes", processes.map((process) => [
    process.gpu_index, process.username, process.pid, formatBytes(process.gpu_memory_bytes),
    formatPercent(process.cpu_percent), formatBytes(process.memory_rss_bytes),
    formatDuration(process.runtime_seconds), process.command || process.executable,
  ]));
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
updateLive();
refreshHistory();
setInterval(updateLive, 2000);
setInterval(refreshHistory, 30000);
