import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { devicesApi } from "../api/modules/devices";
import {
  DESKTOP_WORKSPACE_READY,
  type DesktopWorkspaceState,
} from "../api/desktopWorkspace";
import { useCurrentUser } from "../hooks/useCurrentUser";
import { getAuthToken } from "../api/request";
import { tokenSessionIdentity } from "../utils/authSession";

interface WorkspaceContext {
  available: boolean;
  enabled: boolean | null;
  state: DesktopWorkspaceState | null;
  busy: boolean;
  error: string;
  choose: () => Promise<void>;
  clear: () => Promise<void>;
  retry: () => void;
}

const Context = createContext<WorkspaceContext>({
  available: false,
  enabled: null,
  state: null,
  busy: false,
  error: "",
  choose: async () => {},
  clear: async () => {},
  retry: () => {},
});

export function DesktopWorkspaceProvider({
  children,
}: {
  children: ReactNode;
}) {
  const user = useCurrentUser();
  const owner = user?.id;
  const [available, setAvailable] = useState(
    Boolean(window.mossworkDesktopWorkspace),
  );
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [state, setState] = useState<DesktopWorkspaceState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const working = useRef(false);

  useEffect(() => {
    const ready = () => setAvailable(true);
    window.addEventListener(DESKTOP_WORKSPACE_READY, ready);
    return () => window.removeEventListener(DESKTOP_WORKSPACE_READY, ready);
  }, []);

  useEffect(() => {
    const bridge = window.mossworkDesktopWorkspace;
    if (!available || !bridge || !owner) return;
    let cancelled = false;
    let attempted = false;
    let nextAttempt = 0;
    let delay = 1000;
    const session = tokenSessionIdentity(getAuthToken());
    const sameSession = () =>
      !cancelled && tokenSessionIdentity(getAuthToken()) === session;
    const refresh = async () => {
      if (working.current) return;
      working.current = true;
      try {
        let next = await bridge.status();
        if (cancelled) return;
        if (next.owner_user_id && next.owner_user_id !== owner) {
          next = await bridge.disconnect();
          if (cancelled) return;
        }
        if (!attempted) {
          attempted = true;
          const devices = await devicesApi.list();
          if (cancelled) return;
          setEnabled(devices.enabled);
          if (
            devices.enabled &&
            !next.renew_at &&
            !next.pair_blocked &&
            ["unpaired", "disconnected"].includes(next.state)
          ) {
            setBusy(true);
            const pairing = await devicesApi.pair();
            if (!sameSession()) return;
            next = await bridge.pair(pairing.code);
          }
        }
        if (cancelled) return;
        if (
          bridge.renew &&
          next.renew_at &&
          next.owner_user_id === owner &&
          (next.state === "online" || next.state === "disconnected") &&
          Date.now() >= next.renew_at * 1000 &&
          Date.now() >= nextAttempt &&
          sameSession()
        ) {
          nextAttempt = Date.now() + delay + Math.random() * 500;
          delay = Math.min(delay * 2, 30000);
          try {
            const { ticket, key_hint } = await devicesApi.renewal(
              next.device_id,
            );
            if (!sameSession()) return;
            if (ticket)
              next = await bridge.renew(
                key_hint ? `${ticket}.${key_hint}` : ticket,
              );
          } catch {
            if (!sameSession()) return;
            if (next.state === "disconnected")
              next = { ...next, error: "connection_failed" };
          }
        } else if (next.renew_at && Date.now() < next.renew_at * 1000) {
          delay = 1000;
          nextAttempt = 0;
        }
        if (!sameSession()) return;
        if (next.owner_user_id && next.owner_user_id !== owner) {
          await bridge.disconnect();
          throw new Error("session_changed");
        }
        setState(next);
        setError(
          (previous) =>
            next.error ??
            (next.pair_blocked ? "device_rejected" : undefined) ??
            (next.state === "online" && previous === "connection_failed"
              ? ""
              : previous),
        );
      } catch {
        if (!cancelled) setError("connection_failed");
      } finally {
        working.current = false;
        if (!cancelled) setBusy(false);
      }
    };
    setState(null);
    setError("");
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    window.addEventListener("online", refresh);
    window.addEventListener("focus", refresh);
    window.addEventListener("mosswork:device-lease-due", refresh);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("online", refresh);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("mosswork:device-lease-due", refresh);
    };
  }, [available, owner, revision]);

  const change = useCallback(
    async (action: "choose" | "clear") => {
      const bridge = window.mossworkDesktopWorkspace;
      if (!bridge || working.current) return;
      working.current = true;
      setBusy(true);
      setError("");
      try {
        const next = await bridge[action]();
        if (next.owner_user_id !== owner) throw new Error("session_changed");
        setState(next);
        setError(next.error ?? "");
      } catch (err) {
        const code = err instanceof Error ? err.message : "save_failed";
        setError(
          [
            "directory_unavailable",
            "save_failed",
            "device_offline",
            "session_changed",
          ].includes(code)
            ? code
            : "connection_failed",
        );
      } finally {
        working.current = false;
        setBusy(false);
      }
    },
    [owner],
  );

  return (
    <Context.Provider
      value={{
        available,
        enabled,
        state,
        busy,
        error,
        choose: () => change("choose"),
        clear: () => change("clear"),
        retry: () => setRevision((r) => r + 1),
      }}
    >
      {children}
    </Context.Provider>
  );
}

export function useDesktopWorkspace() {
  return useContext(Context);
}
