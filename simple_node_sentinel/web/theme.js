"use strict";

(() => {
  const storageKey = "sentinel-theme";
  const root = document.documentElement;
  let theme = "light";
  try {
    if (localStorage.getItem(storageKey) === "dark") theme = "dark";
  } catch {
    // The switch still works when browser storage is unavailable.
  }
  // Run before styles load so a saved theme does not flash on startup.
  root.dataset.theme = theme;

  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    const updateButton = () => {
      const dark = root.dataset.theme === "dark";
      button.setAttribute("aria-checked", String(dark));
      button.title = dark ? "Switch to light mode" : "Switch to dark mode";
      button.querySelector(".theme-label").textContent = dark ? "Dark" : "Light";
    };
    button.addEventListener("click", () => {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(storageKey, root.dataset.theme);
      } catch {
        // Keep the selected theme for this page even without persistence.
      }
      updateButton();
    });
    updateButton();
  });
})();
