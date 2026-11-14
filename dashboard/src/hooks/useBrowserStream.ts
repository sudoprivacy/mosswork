import { useState, useRef, useCallback, useEffect } from "react";
import {
  AUTH_TOKEN_CHANGED_EVENT,
  UNAUTHORIZED_EVENT,
  getAuthToken,
} from "../api/request";
import { normalizeUrl } from "../utils/normalizeUrl";
import {
  closeBrowserTabOptimistic,
  markBrowserTabActive,
  mergeBrowserTabsStable,
} from "../utils/browserTabs";

export type BrowserStreamState =
  | "idle"
  | "connecting"
  | "browser_started"
  | "streaming"
  | "stopped"
  | "error";

export interface BrowserTab {
  id: number | string;
  url: string;
  title: string;
  active: boolean;
}

interface BrowserStreamCallbacks {
  onFrame: (base64Data: string, isCurrent: () => boolean) => Promise<boolean>;
  onStatusChange?: (status: BrowserStreamState) => void;
  onError?: (message: string) => void;
}

interface ConnectOptions {
  /** Harness profile for the current user. The WebSocket also binds from JWT. */
  sessionId?: string | null;
  /** When set, screencast is relayed to the peer mosswork via Bridge. */
  bridgeConnectionId?: string | null;
}

function buildWsUrl(
  width: number,
  height: number,
  bridgeConnectionId?: string | null,
): string {
  const token = getAuthToken();
  const path = bridgeConnectionId
    ? `/bridge/connections/${encodeURIComponent(
        bridgeConnectionId,
      )}/browser-stream/ws`
    : "/browser-stream/ws";
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const base = `${protocol}://${window.location.host}/api${path}`;
  const params = new URLSearchParams({
    width: String(width),
    height: String(height),
  });
  if (token) params.set("token", token);
  return `${base}?${params.toString()}`;
}

/**
 * Manages a remote browser streaming WebSocket session.
 *
 * Handles connection lifecycle, frame reception, user input forwarding,
 * and multi-tab management.
 */
/** Lightweight session info pushed by the backend on state changes. */
export interface StreamSessionInfo {
  session_id: string;
  state: string;
  control_owner: "agent" | "user";
  current_url: string;
  conversation_id: string;
  channel_source: string;
  controlEpoch?: number;
  targetEpoch?: number;
}

interface FrameEpoch {
  controlEpoch: number;
  targetEpoch: number;
}

function frameEpoch(message: Record<string, unknown>): FrameEpoch | null {
  const { controlEpoch, targetEpoch } = message;
  return typeof controlEpoch === "number" &&
    Number.isSafeInteger(controlEpoch) &&
    controlEpoch >= 0 &&
    typeof targetEpoch === "number" &&
    Number.isSafeInteger(targetEpoch) &&
    targetEpoch >= 0
    ? { controlEpoch, targetEpoch }
    : null;
}

export function useBrowserStream() {
  const wsRef = useRef<WebSocket | null>(null);
  const callbacksRef = useRef<BrowserStreamCallbacks | null>(null);
  const statusRef = useRef<BrowserStreamState>("idle");
  const frameEpochRef = useRef<FrameEpoch | null>(null);
  const pendingTargetRef = useRef<FrameEpoch | null>(null);
  const releaseEpochRef = useRef<FrameEpoch | null>(null);
  const controlOwnerRef = useRef<"agent" | "user">("agent");
  const [status, setStatus] = useState<BrowserStreamState>("idle");
  const [inputReady, setInputReady] = useState(false);
  const [currentUrl, setCurrentUrl] = useState("");
  const [tabs, setTabs] = useState<BrowserTab[]>([]);
  const [sessionInfo, setSessionInfo] = useState<StreamSessionInfo | null>(
    null,
  );

  const updateStatus = useCallback((s: BrowserStreamState) => {
    if (s === "error" || s === "stopped" || s === "idle") {
      frameEpochRef.current = null;
      pendingTargetRef.current = null;
      setInputReady(false);
    }
    statusRef.current = s;
    setStatus(s);
    callbacksRef.current?.onStatusChange?.(s);
  }, []);

  /** Connect WebSocket and start the browser. */
  const connect = useCallback(
    (
      url: string,
      width: number,
      height: number,
      callbacks: BrowserStreamCallbacks,
      options: ConnectOptions = {},
    ) => {
      callbacksRef.current = callbacks;
      frameEpochRef.current = null;
      pendingTargetRef.current = null;
      releaseEpochRef.current = null;
      setInputReady(false);
      controlOwnerRef.current = "agent";
      setSessionInfo(null);
      updateStatus("connecting");

      // Close any existing connection
      if (wsRef.current && wsRef.current.readyState < WebSocket.CLOSING) {
        wsRef.current.close();
      }
      wsRef.current = null;
      if (!getAuthToken()) {
        updateStatus("idle");
        return;
      }

      const wsUrl = buildWsUrl(width, height, options.bridgeConnectionId);
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl);
      } catch (err) {
        console.error("[BrowserStream] Failed to create WebSocket:", err);
        updateStatus("error");
        return;
      }

      let latestEpoch: FrameEpoch | null = null;
      let frameSequence = 0;
      const acceptEpoch = (epoch: FrameEpoch): boolean => {
        if (
          latestEpoch &&
          (epoch.controlEpoch < latestEpoch.controlEpoch ||
            epoch.targetEpoch < latestEpoch.targetEpoch)
        )
          return false;
        const pending = pendingTargetRef.current;
        if (
          pending &&
          (epoch.controlEpoch > pending.controlEpoch ||
            epoch.targetEpoch > pending.targetEpoch)
        ) {
          pendingTargetRef.current = null;
        }
        if (
          !latestEpoch ||
          epoch.controlEpoch !== latestEpoch.controlEpoch ||
          epoch.targetEpoch !== latestEpoch.targetEpoch
        ) {
          latestEpoch = epoch;
          frameEpochRef.current = null;
          setInputReady(false);
          frameSequence += 1;
        }
        return true;
      };

      ws.onopen = () => {
        if (wsRef.current !== ws) return;
        const startMsg: Record<string, unknown> = {
          type: "start",
          url,
          width,
          height,
          reuse_session: true,
        };
        if (options.sessionId) {
          startMsg.session_id = options.sessionId;
        }
        ws.send(JSON.stringify(startMsg));
        setCurrentUrl(url);
      };

      ws.onmessage = (ev) => {
        if (wsRef.current !== ws) return;
        try {
          const msg = JSON.parse(ev.data as string) as {
            type: string;
            data?: string;
            status?: string;
            message?: string;
            tabs?: BrowserTab[];
            controlEpoch?: number;
            targetEpoch?: number;
            recoverable?: boolean;
          };

          if (msg.type === "frame" && msg.data) {
            const epoch = frameEpoch(msg as Record<string, unknown>);
            if (!epoch && latestEpoch) return;
            if (epoch && !acceptEpoch(epoch)) return;
            const sequence = ++frameSequence;
            const isCurrent = () =>
              wsRef.current === ws &&
              sequence === frameSequence &&
              Boolean(getAuthToken());
            // Decoding is asynchronous. Authorize input only after these exact
            // pixels have been painted; stale decodes cannot overwrite them.
            void callbacksRef.current
              ?.onFrame(msg.data, isCurrent)
              .then((painted) => {
                if (painted && isCurrent() && !pendingTargetRef.current) {
                  frameEpochRef.current = epoch;
                  releaseEpochRef.current = epoch;
                  setInputReady(
                    epoch !== null && controlOwnerRef.current === "user",
                  );
                }
              })
              .catch(() => {
                if (isCurrent()) {
                  frameEpochRef.current = null;
                  setInputReady(false);
                }
              });
          } else if (msg.type === "status" && msg.status) {
            updateStatus(msg.status as BrowserStreamState);
          } else if (msg.type === "tabs" && msg.tabs) {
            setTabs((prevTabs) =>
              mergeBrowserTabsStable(prevTabs, msg.tabs ?? []),
            );
            const activeTab = msg.tabs.find((t) => t.active);
            if (activeTab) setCurrentUrl(activeTab.url);
          } else if (msg.type === "error") {
            pendingTargetRef.current = null;
            frameEpochRef.current = null;
            frameSequence += 1;
            setInputReady(false);
            console.error("[BrowserStream] Server error:", msg.message);
            callbacksRef.current?.onError?.(msg.message ?? "Unknown error");
            if (!msg.recoverable) updateStatus("error");
          } else if (msg.type === "session_update") {
            const epoch = frameEpoch(msg as Record<string, unknown>);
            if (epoch && !acceptEpoch(epoch)) return;
            // Real-time session state pushed by the backend
            const info: StreamSessionInfo = {
              session_id:
                ((msg as Record<string, unknown>).session_id as string) ?? "",
              state: ((msg as Record<string, unknown>).state as string) ?? "",
              control_owner:
                ((msg as Record<string, unknown>).control_owner as
                  | "agent"
                  | "user") ?? "agent",
              current_url:
                ((msg as Record<string, unknown>).current_url as string) ?? "",
              conversation_id:
                ((msg as Record<string, unknown>).conversation_id as string) ??
                "",
              channel_source:
                ((msg as Record<string, unknown>).channel_source as string) ??
                "dashboard",
              controlEpoch: msg.controlEpoch,
              targetEpoch: msg.targetEpoch,
            };
            controlOwnerRef.current = info.control_owner;
            setInputReady(
              frameEpochRef.current !== null && info.control_owner === "user",
            );
            setSessionInfo(info);
          }
        } catch {
          // Non-JSON frames — ignore
        }
      };

      ws.onerror = () => {
        if (wsRef.current !== ws) return;
        updateStatus("error");
      };

      ws.onclose = () => {
        if (wsRef.current !== ws) return;
        frameEpochRef.current = null;
        pendingTargetRef.current = null;
        releaseEpochRef.current = null;
        setInputReady(false);
        if (
          statusRef.current !== "stopped" &&
          statusRef.current !== "idle" &&
          statusRef.current !== "error"
        ) {
          updateStatus("stopped");
        }
        // Don't clear tabs on close — this preserves the tab bar state
        // during auto-reconnect so it doesn't flash empty.
      };

      wsRef.current = ws;
    },
    [updateStatus],
  );

  /** Send a user input event to the remote browser. */
  const sendEvent = useCallback((event: Record<string, unknown>): boolean => {
    if (!getAuthToken()) return false;
    if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
    // Bind coordinates to the pixels actually shown, including after reconnect.
    // Stopping this viewer does not operate on the remote browser.
    const release = ["mouseup", "keyup", "compositionend"].includes(
      String(event.type),
    );
    const epoch =
      frameEpochRef.current ?? (release ? releaseEpochRef.current : null);
    if (event.type !== "stop" && !epoch) return false;
    if (event.type !== "stop" && controlOwnerRef.current !== "user")
      return false;
    wsRef.current.send(JSON.stringify({ ...event, ...epoch }));
    if (
      [
        "navigate",
        "goback",
        "goforward",
        "reload",
        "tab_switch",
        "tab_close",
        "tab_new",
        "resize",
      ].includes(String(event.type))
    ) {
      // Reserve the displayed target immediately. Even a frame already in
      // flight cannot authorize input while this target change is pending.
      pendingTargetRef.current = epoch;
      frameEpochRef.current = null;
      setInputReady(false);
    }
    return true;
  }, []);

  /** Navigate to a new URL. */
  const navigate = useCallback(
    (url: string) => {
      const target = normalizeUrl(url);
      if (!target) return;
      if (sendEvent({ type: "navigate", url: target })) setCurrentUrl(target);
    },
    [sendEvent],
  );

  /** Switch to a tab by id. */
  const switchTab = useCallback(
    (tabId: number | string) => {
      if (sendEvent({ type: "tab_switch", tab_id: tabId })) {
        setTabs((prevTabs) => markBrowserTabActive(prevTabs, tabId));
      }
    },
    [sendEvent],
  );

  /** Close a tab by id. */
  const closeTab = useCallback(
    (tabId: number | string) => {
      if (sendEvent({ type: "tab_close", tab_id: tabId })) {
        setTabs((prevTabs) => closeBrowserTabOptimistic(prevTabs, tabId));
      }
    },
    [sendEvent],
  );

  /** Open a new blank tab. */
  const newTab = useCallback(() => {
    sendEvent({ type: "tab_new" });
  }, [sendEvent]);

  /** Stop the remote browser. */
  const stop = useCallback(() => {
    sendEvent({ type: "stop" });
    updateStatus("stopped");
  }, [sendEvent, updateStatus]);

  /** Disconnect WebSocket completely. */
  const disconnect = useCallback(() => {
    frameEpochRef.current = null;
    pendingTargetRef.current = null;
    releaseEpochRef.current = null;
    setInputReady(false);
    const socket = wsRef.current;
    // Invalidate asynchronous paint and late callbacks before closing the
    // transport. A closing socket is not evidence of remote writer cleanup.
    wsRef.current = null;
    if (socket) {
      try {
        socket.close();
      } catch {
        // ignore
      }
    }
    controlOwnerRef.current = "agent";
    updateStatus("idle");
    setTabs([]);
    setCurrentUrl("");
    setSessionInfo(null);
  }, [updateStatus]);

  useEffect(() => {
    // Consume the existing auth contract. Sliding renewal emits the same
    // token-change event, so a nonempty renewed token is not a logout.
    // Canonical organization/resource changes require the host's separate
    // current-grant contract; decoding JWT claims here cannot supply it.
    const checkLogout = () => {
      if (!getAuthToken()) disconnect();
    };
    window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, checkLogout);
    window.addEventListener(UNAUTHORIZED_EVENT, disconnect);
    window.addEventListener("storage", checkLogout);
    window.addEventListener("focus", checkLogout);
    return () => {
      window.removeEventListener(AUTH_TOKEN_CHANGED_EVENT, checkLogout);
      window.removeEventListener(UNAUTHORIZED_EVENT, disconnect);
      window.removeEventListener("storage", checkLogout);
      window.removeEventListener("focus", checkLogout);
    };
  }, [disconnect]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
  }, []);

  return {
    status,
    inputReady,
    currentUrl,
    tabs,
    sessionInfo,
    connect,
    sendEvent,
    navigate,
    switchTab,
    closeTab,
    newTab,
    stop,
    disconnect,
  };
}
