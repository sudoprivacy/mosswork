import i18n from "../i18n";
import { tokenSessionIdentity } from "../utils/authSession";
import { AUTH_TOKEN_CHANGED_EVENT, getAuthToken } from "./request";
import type { DesktopWorkspaceState } from "./desktopWorkspace";

type Observation = { state?: DesktopWorkspaceState; error?: string };
type Observer = (value: Observation) => void;
const observers = new Set<Observer>();
const refreshEvent = "mosswork:device-lease-due";

export function reportDesktopDevice(value: Observation) {
  for (const observer of observers) observer(value);
}

export interface DesktopDeviceWait {
  signal?: AbortSignal;
  onWait?: (phase: "device" | "network") => void;
  onDispatch?: () => void;
}

export async function withReadyDesktopDevice<Result>(
  operation: () => Promise<Result>,
  options: DesktopDeviceWait = {},
): Promise<Result> {
  if (!window.mossworkDesktopWorkspace) return operation();
  const session = tokenSessionIdentity(getAuthToken());
  let path: string | undefined;
  let requestRefresh = true;
  const started = Date.now();
  for (;;) {
    await new Promise<void>((resolve, reject) => {
      const cleanup = () => {
        observers.delete(observe);
        window.removeEventListener(AUTH_TOKEN_CHANGED_EVENT, checkLogin);
        options.signal?.removeEventListener("abort", abort);
        window.clearTimeout(timer);
      };
      const abort = () => {
        cleanup();
        reject(new DOMException("Aborted", "AbortError"));
      };
      const observe: Observer = ({ state, error }) => {
        const reason =
          tokenSessionIdentity(getAuthToken()) !== session
            ? "session_changed"
            : path !== undefined && state && state.path !== path
            ? "workspace_changed"
            : error;
        if (reason) {
          cleanup();
          reject(new Error(i18n.t(`devices.workspaceError_${reason}`)));
          return;
        }
        if (state?.path) path ??= state.path;
        if (
          state?.state === "online" &&
          navigator.onLine !== false &&
          (!state.expires_at || state.expires_at * 1000 > Date.now())
        ) {
          cleanup();
          resolve();
        }
      };
      const checkLogin = () => observe({});
      const timer = window.setTimeout(
        () => options.onWait?.("network"),
        Math.max(0, 30000 - (Date.now() - started)),
      );
      observers.add(observe);
      window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, checkLogin);
      options.onWait?.(Date.now() - started >= 30000 ? "network" : "device");
      options.signal?.addEventListener("abort", abort, { once: true });
      if (options.signal?.aborted) abort();
      else if (requestRefresh) window.dispatchEvent(new Event(refreshEvent));
    });
    if (options.signal?.aborted)
      throw new DOMException("Aborted", "AbortError");
    if (tokenSessionIdentity(getAuthToken()) !== session)
      throw new Error(i18n.t("devices.workspaceError_session_changed"));
    options.onDispatch?.();
    try {
      return await operation();
    } catch (error) {
      if ((error as { stage?: string })?.stage !== "device_preparing")
        throw error;
      requestRefresh = false;
    }
  }
}
