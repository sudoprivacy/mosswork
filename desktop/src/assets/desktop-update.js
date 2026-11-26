import { Call, Events } from "/wails/runtime.js";

let copy = {};
let isDisposed = false;
const element = (id) => document.getElementById(id);
const restartDialog = element("restart-dialog");

async function refresh() {
  const view = await Call.ByName("main.App.GetDesktopUpdate");
  if (isDisposed) return;
  copy = view.strings;
  const status = view.status;
  document.documentElement.lang = view.locale === "zh" ? "zh-CN" : "en";
  document.title = copy.title;
  for (const id of [
    "title",
    "note",
    "check",
    "download",
    "restart",
    "confirm",
    "cancel",
  ]) {
    element(id).textContent = copy[id];
  }
  element("confirm-restart").textContent = copy.restart;
  element("versions").textContent =
    `${copy.current}: ${status.current_version}` +
    (status.latest_version
      ? ` · ${copy.latest}: ${status.latest_version}`
      : "");
  element("platform").textContent = `${copy.platform}: ${view.platform}`;
  element("server").textContent = view.server
    ? `${copy.server}: ${view.server}`
    : copy["no-server"];
  const unconfigured =
    status.current_version === "dev" ? copy.development : copy.unconfigured;
  element("status").textContent =
    status.state === "unconfigured"
      ? unconfigured
      : status.error
      ? ["busy", "location"].includes(status.error_stage)
        ? copy[status.error_stage]
        : copy.error
      : copy[status.state] || copy.idle;
  const isBusy = [
    "checking",
    "downloading",
    "verifying",
    "installing",
  ].includes(status.state);
  element("check").disabled =
    isBusy || status.state === "unconfigured" || !view.server;
  element("check").title =
    status.state === "unconfigured"
      ? unconfigured
      : !view.server
      ? copy["no-server"]
      : isBusy
      ? copy[status.state]
      : "";
  element("download").hidden =
    !status.latest_version || status.is_ready || isBusy;
  element("restart").hidden = !status.is_ready;
  if (!status.is_ready && restartDialog.open) restartDialog.close();
  const progress = element("progress");
  progress.hidden = status.state !== "downloading";
  progress.max = status.total || 1;
  progress.value = status.written || 0;
}

function dispatchAction(action) {
  Events.Emit("desktop:update:" + action);
  requestRefresh();
}

element("check").onclick = () => dispatchAction("check");
element("download").onclick = () => dispatchAction("download");
element("restart").onclick = () => restartDialog.showModal();
element("cancel").onclick = () => restartDialog.close();
element("confirm-restart").onclick = () => {
  if (!restartDialog.open) return;
  restartDialog.close();
  dispatchAction("restart");
};

function requestRefresh() {
  void refresh().catch(() => {
    if (!isDisposed) element("status").textContent = copy.error || "";
  });
}

const unsubscribe = Events.On("desktop:update:changed", requestRefresh);
await refresh();
window.addEventListener(
  "pagehide",
  () => {
    isDisposed = true;
    unsubscribe();
  },
  { once: true },
);
