"use strict";

const $ = (selector) => document.querySelector(selector);

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

function card(title, rows) {
  const element = textElement("article", "card", "");
  element.appendChild(textElement("h3", "", title));
  for (const [label, value] of rows) {
    const row = textElement("div", "metric", "");
    row.appendChild(textElement("span", "label", label));
    row.appendChild(textElement("strong", "", value));
    element.appendChild(row);
  }
  return element;
}

function replaceCards(selector, cards) {
  const container = $(selector);
  container.replaceChildren(...cards);
  if (!cards.length) container.appendChild(textElement("p", "empty", "No data available"));
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
  for (const values of rows) {
    const row = document.createElement("tr");
    for (const value of values) row.appendChild(textElement("td", "", String(value)));
    body.appendChild(row);
  }
}

async function request(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

function renderSystem(data) {
  const cpu = data.cpu || {};
  const temperature = data.cpu_temperature || {};
  const memory = data.memory || {};
  const swap = data.swap || {};
  const load = cpu.load_average || {};
  replaceCards("#system-summary", [
    card("CPU", [
      ["Usage", formatPercent(cpu.usage_percent)],
      ["Maximum temperature", formatTemperature(temperature.max_celsius)],
      ["Logical cores", cpu.logical_count ?? "N/A"],
      ["Load 1 / 5 / 15", [load["1m"], load["5m"], load["15m"]].map((v) => v == null ? "N/A" : Number(v).toFixed(2)).join(" / ")],
      ["Uptime", cpu.boot_time ? formatDuration(Date.now() / 1000 - cpu.boot_time) : "N/A"],
    ]),
    card("Memory", [
      ["Used", `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)}`],
      ["Available", formatBytes(memory.available_bytes)],
      ["Usage", formatPercent(memory.usage_percent)],
    ]),
    card("Swap", [
      ["Used", `${formatBytes(swap.used_bytes)} / ${formatBytes(swap.total_bytes)}`],
      ["Usage", formatPercent(swap.usage_percent)],
      ["Sampled", data.sampled_at ? new Date(data.sampled_at * 1000).toLocaleString() : "Waiting"],
    ]),
  ]);
}

function renderGpus(gpus) {
  replaceCards("#gpus", gpus.map((gpu) => card(`GPU ${gpu.index}: ${gpu.name}`, [
    ["Temperature", formatTemperature(gpu.temperature_celsius)],
    ["Utilization", formatPercent(gpu.utilization_percent)],
    ["Memory", `${formatBytes(gpu.memory_used_bytes)} / ${formatBytes(gpu.memory_total_bytes)}`],
    ["Fan", formatPercent(gpu.fan_percent)],
    ["Power", gpu.power_watts == null ? "N/A" : `${gpu.power_watts.toFixed(1)} / ${gpu.power_limit_watts?.toFixed(1) ?? "N/A"} W`],
    ["Processes", String(gpu.process_count)],
    ["Users", (gpu.users || []).join(", ") || "None"],
  ])));
}

async function refresh() {
  const [summary, gpus, processes, users, disks, alerts] = await Promise.all([
    request("/api/summary"), request("/api/gpus"), request("/api/gpu-processes"),
    request("/api/users"), request("/api/disks"), request("/api/alerts"),
  ]);
  renderSystem(summary);
  renderGpus(gpus);
  replaceRows("#gpu-processes", processes.map((p) => [
    p.gpu_index, p.username, p.pid, formatBytes(p.gpu_memory_bytes),
    formatPercent(p.cpu_percent), formatBytes(p.memory_rss_bytes),
    formatDuration(p.runtime_seconds), p.command || p.executable,
  ]));
  replaceRows("#users", users.map((u) => [
    u.username, u.process_count, formatPercent(u.cpu_percent),
    formatBytes(u.memory_rss_bytes), u.gpu_process_count, formatBytes(u.gpu_memory_bytes),
  ]));
  replaceRows("#disks", disks.map((d) => [
    d.device, d.mountpoint, d.filesystem, formatBytes(d.total_bytes),
    formatBytes(d.used_bytes), formatBytes(d.available_bytes), formatPercent(d.usage_percent),
  ]));
  replaceRows("#alerts", alerts.map((a) => [
    a.gpu_index, new Date(a.triggered_at * 1000).toLocaleString(), a.status,
    formatTemperature(a.current_temperature), formatTemperature(a.max_temperature),
    (a.users || []).join(", ") || "None", a.email_status || "not attempted",
  ]));
  $("#connection").textContent = "Live";
  $("#connection").className = "status live";
}

async function update() {
  try {
    await refresh();
  } catch (error) {
    $("#connection").textContent = `Unavailable: ${error.message}`;
    $("#connection").className = "status error";
  }
}

update();
setInterval(update, 2000);
