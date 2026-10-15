(() => {
  "use strict";
  const allowed = new Set([
    "openBusinessModal", "openSettingsModal", "closeBusinessModal", "closeSettingsModal",
    "showAddAdminTab", "showEditAdminTab", "submitAddAdmin", "submitEditAdmin",
    "switchTab", "openStaffModal", "closeStaffModal", "closeResetModal",
    "toggleCamera", "toggleManualInput", "searchManualToken", "processTransaction",
    "processPointsEarning", "processPointsRedeem", "resetCustomerSession",
  ]);

  function parseArgument(value, event) {
    const item = value.trim();
    if (item === "event") return event;
    if (/^-?\d+(\.\d+)?$/.test(item)) return Number(item);
    const quoted = item.match(/^(['"])(.*)\1$/);
    return quoted ? quoted[2] : undefined;
  }

  function bind(attribute, eventName) {
    document.querySelectorAll(`[${attribute}]`).forEach((element) => {
      const source = element.getAttribute(attribute).trim().replace(/^return\s+/, "").replace(/;$/, "");
      const match = source.match(/^([A-Za-z_$][\w$]*)\((.*)\)$/);
      if (!match || !allowed.has(match[1])) return;
      element.removeAttribute(attribute);
      element.addEventListener(eventName, (event) => {
        const fn = window[match[1]];
        if (typeof fn !== "function") return;
        const args = match[2].trim() ? match[2].split(",").map((value) => parseArgument(value, event)) : [];
        if (args.some((value) => value === undefined) || fn(...args) === false) event.preventDefault();
      });
    });
  }

  bind("onclick", "click");
  bind("onsubmit", "submit");
})();
