import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(
  new URL("../../dashboard/package.json", import.meta.url),
);
const { JSDOM } = require("jsdom");
const html = await readFile(
  new URL("../src/assets/desktop-update.html", import.meta.url),
  "utf8",
);
const script = (
  await readFile(
    new URL("../src/assets/desktop-update.js", import.meta.url),
    "utf8",
  )
).replace(
  'import { Call, Events } from "/wails/runtime.js";',
  "const { Call, Events } = window.native;",
);

async function panel(status, server = "https://company.example") {
  const dom = new JSDOM(html, { runScripts: "outside-only" });
  const { window } = dom;
  const emitted = [];
  let listener;
  let current = status;
  // jsdom exposes the dialog element but does not implement its modal methods.
  const dialog = window.document.getElementById("restart-dialog");
  dialog.showModal = () => {
    dialog.open = true;
  };
  dialog.close = () => {
    dialog.open = false;
  };
  // Labels are supplied by native localization, not inferred from the server
  // dashboard, release notes, or the remote company account.
  const strings = {
    title: "Client updates",
    current: "Current",
    latest: "Latest",
    check: "Check",
    download: "Download",
    restart: "Restart",
    ready: "Ready",
    busy: "Finish local tasks first",
    available: "Available",
    error: "Update failed",
    note: "Keep work",
    confirm: "Restart?",
    cancel: "Cancel",
    server: "Update server",
    platform: "Platform",
    development: "Development builds do not support online updates.",
    unconfigured: "Use a stable company installer.",
    "no-server": "Set a server address first.",
    idle: "Check for a new client version.",
  };
  window.native = {
    Call: {
      ByName: async (method) => {
        assert.equal(method, "main.App.GetDesktopUpdate");
        return {
          locale: "en",
          strings,
          status: current,
          server,
          platform: "windows / amd64",
        };
      },
    },
    Events: {
      On(name, callback) {
        assert.equal(name, "desktop:update:changed");
        listener = callback;
        return () => {};
      },
      Emit(name) {
        emitted.push(name);
      },
    },
  };
  await window.eval(`(async () => { ${script} })()`);
  return {
    dom,
    emitted,
    byId: (id) => window.document.getElementById(id),
    close() {
      window.dispatchEvent(new window.Event("pagehide"));
      window.close();
    },
    async change(next) {
      current = next;
      listener();
      await new Promise((resolve) => setImmediate(resolve));
    },
  };
}

test("available update requires an explicit download and a separate confirmed restart", async () => {
  const p = await panel({
    state: "available",
    current_version: "0.0.7",
    latest_version: "0.0.8",
  });
  try {
    assert.deepEqual(p.emitted, []);
    assert.equal(
      p.byId("versions").textContent,
      "Current: 0.0.7 · Latest: 0.0.8",
    );
    assert.equal(
      p.byId("server").textContent,
      "Update server: https://company.example",
    );
    assert.equal(p.byId("platform").textContent, "Platform: windows / amd64");
    assert.equal(p.byId("download").hidden, false);
    assert.equal(p.byId("restart").hidden, true);
    p.byId("download").click();
    assert.deepEqual(p.emitted, ["desktop:update:download"]);
    await p.change({
      state: "ready",
      current_version: "0.0.7",
      latest_version: "0.0.8",
      is_ready: true,
    });
    p.byId("restart").click();
    assert.equal(p.byId("restart-dialog").open, true);
    assert.equal(p.byId("confirm").textContent, "Restart?");
    assert.equal(p.byId("cancel").textContent, "Cancel");
    assert.equal(p.byId("confirm-restart").textContent, "Restart");
    p.byId("cancel").click();
    assert.equal(p.byId("restart-dialog").open, false);
    assert.equal(
      p.emitted.length,
      1,
      "cancelled confirmation must not restart",
    );
    p.byId("restart").click();
    p.byId("confirm-restart").click();
    assert.equal(p.byId("restart-dialog").open, false);
    p.byId("confirm-restart").click();
    assert.deepEqual(p.emitted, [
      "desktop:update:download",
      "desktop:update:restart",
    ]);
  } finally {
    p.close();
  }
});

test("a candidate becoming unavailable dismisses its restart confirmation", async () => {
  const p = await panel({ state: "ready", is_ready: true });
  try {
    p.byId("restart").click();
    await p.change({ state: "idle", is_ready: false });
    assert.equal(p.byId("restart-dialog").open, false);
    p.byId("confirm-restart").click();
    assert.deepEqual(p.emitted, []);
  } finally {
    p.close();
  }
});

test("download progress is event driven and busy rejection preserves the ready candidate", async () => {
  const p = await panel({
    state: "downloading",
    current_version: "0.0.7",
    written: 20,
    total: 100,
  });
  try {
    assert.equal(p.byId("check").disabled, true);
    assert.equal(p.byId("progress").value, 20);
    await p.change({
      state: "ready",
      is_ready: true,
      current_version: "0.0.7",
      error: "active task",
      error_stage: "busy",
    });
    assert.equal(p.byId("status").textContent, "Finish local tasks first");
    assert.equal(p.byId("restart").hidden, false);
    assert.deepEqual(p.emitted, []);
  } finally {
    p.close();
  }
});

test("development builds explain the disabled check without emitting an update action", async () => {
  const p = await panel({ state: "unconfigured", current_version: "dev" });
  try {
    assert.equal(p.byId("check").disabled, true);
    assert.equal(
      p.byId("status").textContent,
      "Development builds do not support online updates.",
    );
    assert.equal(p.byId("check").title, p.byId("status").textContent);
    p.byId("check").click();
    assert.deepEqual(p.emitted, []);
  } finally {
    p.close();
  }
});

test("stable builds can check through a configured server and explain a missing address", async () => {
  const p = await panel({ state: "idle", current_version: "0.0.7" });
  try {
    assert.equal(p.byId("check").disabled, false);
    p.byId("check").click();
    assert.deepEqual(p.emitted, ["desktop:update:check"]);
  } finally {
    p.close();
  }
  const missing = await panel({ state: "idle", current_version: "0.0.7" }, "");
  try {
    assert.equal(missing.byId("check").disabled, true);
    assert.equal(missing.byId("check").title, "Set a server address first.");
    missing.byId("check").click();
    assert.deepEqual(missing.emitted, []);
  } finally {
    missing.close();
  }
});

test("about renders opaque backgrounds and contrasting text in both themes", async () => {
  const p = await panel({ state: "unconfigured", current_version: "dev" });
  try {
    const { window } = p.dom;
    const body = window.getComputedStyle(window.document.body);
    assert.equal(body.backgroundColor, "rgb(245, 246, 248)");
    assert.equal(body.color, "rgb(28, 28, 30)");
    const button = window.getComputedStyle(p.byId("check"));
    assert.equal(button.backgroundColor, "rgb(255, 255, 255)");
    assert.equal(button.color, "rgb(28, 28, 30)");
    const dark = Array.from(window.document.styleSheets[0].cssRules).find(
      (rule) => rule.conditionText === "(prefers-color-scheme: dark)",
    );
    assert.ok(dark);
    const darkBody = dark.cssRules[0].style;
    assert.equal(
      darkBody.getPropertyValue("background-color"),
      "rgb(14, 14, 14)",
    );
    assert.equal(darkBody.getPropertyValue("color"), "rgb(240, 240, 240)");
  } finally {
    p.close();
  }
});

test("failed checks display failure copy rather than the check action or busy label", async () => {
  const p = await panel({
    state: "error",
    current_version: "0.0.7",
    error: "HTTP 503",
    error_stage: "check",
  });
  try {
    assert.equal(p.byId("status").textContent, "Update failed");
    assert.equal(p.byId("check").disabled, false);
  } finally {
    p.close();
  }
});
