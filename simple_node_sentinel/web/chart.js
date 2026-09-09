"use strict";

function highlightSeries(container, key) {
  container.querySelectorAll("[data-series]").forEach((node) => {
    node.classList.toggle("is-dimmed", key != null && node.dataset.series !== key);
    node.classList.toggle("is-highlighted", key != null && node.dataset.series === key);
  });
}

class TimeSeriesChart {
  constructor(container, options) {
    this.container = container;
    this.options = options;
    this.points = [];
    this.segments = [];
    this.hoverKey = null;
    this.focusKey = null;
    this.selectedKey = null;
    this.externalKey = null;
    this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.svg.setAttribute("role", "img");
    this.svg.setAttribute("aria-label", options.title || "Metric history");
    this.svg.classList.add("timeseries-svg");
    this.legend = document.createElement("div");
    this.legend.className = "chart-legend";
    this.legendValues = new Map();
    // Keep legend buttons in place during live updates, including keyboard focus.
    options.series.forEach((item) => {
      const label = document.createElement("button");
      label.type = "button";
      label.className = "legend-item";
      label.dataset.series = item.key;
      label.setAttribute("aria-pressed", "false");
      const swatch = document.createElement("i");
      swatch.style.background = item.color;
      const name = document.createElement("span");
      name.className = "legend-label";
      name.textContent = item.label;
      const value = document.createElement("strong");
      value.className = "legend-value";
      label.append(swatch, name, value);
      label.addEventListener("pointerenter", () => this.setHover(item.key));
      label.addEventListener("pointerleave", () => this.setHover(null));
      label.addEventListener("focus", () => {
        this.focusKey = label.matches(":focus-visible") ? item.key : null;
        this.applyHighlight();
      });
      label.addEventListener("blur", () => {
        this.focusKey = null;
        this.applyHighlight();
      });
      label.addEventListener("click", () => {
        this.selectedKey = this.selectedKey === item.key ? null : item.key;
        this.applyHighlight();
      });
      this.legendValues.set(item.key, value);
      this.legend.appendChild(label);
    });
    container.replaceChildren(this.legend, this.svg);
    container.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        this.selectedKey = this.focusKey = this.hoverKey = null;
        this.applyHighlight();
      }
    });
    this.svg.addEventListener("pointermove", (event) => {
      if (event.pointerType === "touch") return;
      const matrix = this.svg.getScreenCTM();
      if (!matrix) return;
      const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
      // Pick the nearest line, even when two series cross or almost overlap.
      let nearest = null;
      let distance = 9 * 9;
      for (const segment of this.segments) {
        const dx = segment.x2 - segment.x1;
        const dy = segment.y2 - segment.y1;
        const length = dx * dx + dy * dy;
        const t = length ? Math.max(0, Math.min(1,
          ((point.x - segment.x1) * dx + (point.y - segment.y1) * dy) / length)) : 0;
        const squared = (point.x - segment.x1 - t * dx) ** 2
          + (point.y - segment.y1 - t * dy) ** 2;
        if (squared < distance) {
          distance = squared;
          nearest = segment.key;
        }
      }
      this.setHover(nearest);
    });
    this.svg.addEventListener("pointerleave", () => this.setHover(null));
    this.observer = new ResizeObserver(() => this.update(this.points));
    this.observer.observe(container);
  }

  setHover(key) {
    if (this.hoverKey === key) return;
    this.hoverKey = key;
    this.applyHighlight();
  }

  setExternalHighlight(key) {
    this.externalKey = key;
    this.applyHighlight();
  }

  applyHighlight() {
    highlightSeries(this.container, this.externalKey ?? this.hoverKey ?? this.focusKey ?? this.selectedKey);
    this.legend.querySelectorAll("button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.series === this.selectedKey));
    });
  }

  destroy() {
    this.observer.disconnect();
  }

  update(points) {
    this.points = points;
    this.segments = [];
    const viewWidth = Math.max(240, this.container.clientWidth);
    this.svg.setAttribute("viewBox", `0 0 ${viewWidth} 160`);
    const series = this.options.series;
    series.forEach((item) => {
      const latest = [...points].reverse()
        .map((point) => this.value(item, point))
        .find((value) => Number.isFinite(value));
      const label = `${item.label}${latest === undefined ? "" : ` ${item.format(latest)}`}`;
      this.legendValues.get(item.key).textContent = latest === undefined ? "—" : item.format(latest);
      this.legendValues.get(item.key).parentElement.setAttribute("aria-label", `Highlight ${label}`);
    });

    this.svg.replaceChildren();
    if (!points.length) {
      this.svg.appendChild(this.text(viewWidth / 2, 86, "Waiting for historical data", "empty-chart"));
      this.applyHighlight();
      return;
    }

    const left = 32;
    const right = 10;
    const top = 12;
    const bottom = 26;
    const width = viewWidth - left - right;
    const height = 160 - top - bottom;
    for (const value of [0, 50, 100]) {
      const y = top + height - (value / 100) * height;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", left);
      line.setAttribute("x2", left + width);
      line.setAttribute("y1", y);
      line.setAttribute("y2", y);
      line.classList.add("chart-grid");
      this.svg.appendChild(line);
      this.svg.appendChild(this.text(left - 7, y + 4, String(value), "chart-axis", "end"));
    }

    const firstTime = Number(points[0].sampled_at);
    const lastTime = Number(points[points.length - 1].sampled_at);
    const span = Math.max(1, lastTime - firstTime);
    for (const item of series) {
      let pathData = "";
      let previous = null;
      points.forEach((point) => {
        const raw = this.value(item, point);
        if (!Number.isFinite(raw)) {
          previous = null;
          return;
        }
        const normalized = Math.max(0, Math.min(100, item.normalize(raw, point)));
        const x = left + ((Number(point.sampled_at) - firstTime) / span) * width;
        const y = top + height - (normalized / 100) * height;
        pathData += `${previous ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;
        if (previous) this.segments.push({key: item.key, x1: previous.x, y1: previous.y, x2: x, y2: y});
        previous = {x, y};
      });
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", pathData.trim());
      path.setAttribute("stroke", item.color);
      path.dataset.series = item.key;
      path.classList.add("chart-line");
      this.svg.appendChild(path);
    }

    this.svg.appendChild(this.text(left, 154,
      new Date(firstTime * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}), "chart-axis", "start"));
    this.svg.appendChild(this.text(left + width, 154,
      new Date(lastTime * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}), "chart-axis", "end"));
    this.applyHighlight();
  }

  value(series, point) {
    const value = series.value ? series.value(point) : point[series.key];
    return value === null || value === undefined ? Number.NaN : Number(value);
  }

  text(x, y, content, className, anchor = "middle") {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "text");
    node.setAttribute("x", x);
    node.setAttribute("y", y);
    node.setAttribute("text-anchor", anchor);
    node.classList.add(className);
    node.textContent = content;
    return node;
  }
}

window.TimeSeriesChart = TimeSeriesChart;
window.highlightChartSeries = highlightSeries;
