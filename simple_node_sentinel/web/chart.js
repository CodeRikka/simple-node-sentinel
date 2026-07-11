"use strict";

class TimeSeriesChart {
  constructor(container, options) {
    this.container = container;
    this.options = options;
    this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.svg.setAttribute("viewBox", "0 0 640 160");
    this.svg.setAttribute("role", "img");
    this.svg.setAttribute("aria-label", options.title || "Metric history");
    this.svg.classList.add("timeseries-svg");
    this.legend = document.createElement("div");
    this.legend.className = "chart-legend";
    container.replaceChildren(this.legend, this.svg);
  }

  update(points) {
    const series = this.options.series;
    this.legend.replaceChildren(...series.map((item) => {
      const label = document.createElement("span");
      label.className = "legend-item";
      const swatch = document.createElement("i");
      swatch.style.background = item.color;
      const latest = [...points].reverse()
        .map((point) => this.value(item, point))
        .find((value) => Number.isFinite(value));
      label.append(swatch, document.createTextNode(
        `${item.label}${latest === undefined ? "" : ` ${item.format(latest)}`}`,
      ));
      return label;
    }));

    this.svg.replaceChildren();
    if (!points.length) {
      this.svg.appendChild(this.text(320, 86, "Waiting for historical data", "empty-chart"));
      return;
    }

    const left = 38;
    const right = 12;
    const top = 12;
    const bottom = 26;
    const width = 640 - left - right;
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
      let drawing = false;
      points.forEach((point) => {
        const raw = this.value(item, point);
        if (!Number.isFinite(raw)) {
          drawing = false;
          return;
        }
        const normalized = Math.max(0, Math.min(100, item.normalize(raw, point)));
        const x = left + ((Number(point.sampled_at) - firstTime) / span) * width;
        const y = top + height - (normalized / 100) * height;
        pathData += `${drawing ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;
        drawing = true;
      });
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", pathData.trim());
      path.setAttribute("stroke", item.color);
      path.classList.add("chart-line");
      this.svg.appendChild(path);
    }

    this.svg.appendChild(this.text(
      left,
      154,
      new Date(firstTime * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      "chart-axis",
      "start",
    ));
    this.svg.appendChild(this.text(
      left + width,
      154,
      new Date(lastTime * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      "chart-axis",
      "end",
    ));
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
