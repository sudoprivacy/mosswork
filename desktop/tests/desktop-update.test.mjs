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

async function panel(status) {
  const dom = new JSDOM(html, { runScripts: "outside-only" });
  const { window } = dom;
  const emitted = [];
  let listener;
  let current = status;
  let confirmed = false;
  window.confirm = () => confirmed;
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
  };
  window.native = {
    Call: {
      ByName: async (method) => {
        assert.equal(method, "main.App.GetDesktopUpdate");
        return { locale: "en", strings, status: current };
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
    confirm: () => {
      confirmed = true;
    },
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
    assert.equal(
      p.emitted.length,
      1,
      "cancelled confirmation must not restart",
    );
    p.confirm();
    p.byId("restart").click();
    assert.deepEqual(p.emitted, [
      "desktop:update:download",
      "desktop:update:restart",
    ]);
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
