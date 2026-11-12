import { Call, Events } from "/wails/runtime.js";

let copy = {};
let isDisposed = false;
const element = (id) => document.getElementById(id);

async function refresh() {
  const view = await Call.ByName("main.App.GetDesktopUpdate");
  if (isDisposed) return;
  copy = view.strings;
  const status = view.status;
  document.documentElement.lang = view.locale === "zh" ? "zh-CN" : "en";
  document.title = copy.title;
  for (const id of ["title", "note", "check", "download", "restart"]) {
    element(id).textContent = copy[id];
  }
  element("versions").textContent =
    `${copy.current}: ${status.current_version}` +
    (status.latest_version
      ? ` · ${copy.latest}: ${status.latest_version}`
      : "");
  element("status").textContent = status.error
    ? copy[status.error_stage] || copy.error
    : copy[status.state] || copy.idle;
  const isBusy = [
    "checking",
    "downloading",
    "verifying",
    "installing",
  ].includes(status.state);
  element("check").disabled = isBusy || status.state === "unconfigured";
  element("download").hidden =
    !status.latest_version || status.is_ready || isBusy;
  element("restart").hidden = !status.is_ready;
  const progress = element("progress");
  progress.hidden = status.state !== "downloading";
  progress.max = status.total || 1;
  progress.value = status.written || 0;
}

for (const action of ["check", "download", "restart"]) {
  element(action).onclick = () => {
    if (action === "restart" && !window.confirm(copy.confirm)) return;
    Events.Emit("desktop:update:" + action);
    requestRefresh();
  };
}

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
