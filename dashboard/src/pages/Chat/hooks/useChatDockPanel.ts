import { useCallback, useEffect, useState } from "react";
import type { PanelMode } from "../../../components/BrowserWorkspace";
import {
  canonicalizeDockFilePath,
  dockFileTabId,
  isHostAbsolutePath,
  normalizeDockFilePath,
} from "../utils/dockFilePath";
import type { KnowledgeCitation } from "../../../utils/parseKnowledgeCitations";
import { dockKnowledgeTabId } from "../utils/dockKnowledgeTabId";
import { dockToolUiTabId } from "../utils/dockToolUiTabId";
import { usePanelResize, type PanelSizes } from "./usePanelResize";

const PANEL_MODE_KEY = "mosswork:chat-dock:mode";
const PANEL_SIZE_KEY = "mosswork:chat-dock:size";
/** Legacy keys — read once for migration. */
const LEGACY_FILE_MODE_KEY = "mosswork:file-panel:mode";
const LEGACY_BROWSER_MODE_KEY = "mosswork:browser-panel:mode";
const LEGACY_FILE_SIZE_KEY = "mosswork:file-panel:size";
const LEGACY_BROWSER_SIZE_KEY = "mosswork:browser-panel:size";

export type DockTab =
  | { id: "files"; kind: "files" }
  | { id: "workspace"; kind: "workspace" }
  | { id: "browser"; kind: "browser" }
  | { id: "terminal"; kind: "terminal" }
  | { id: string; kind: "file"; path: string; agentId?: string }
  | {
      id: string;
      kind: "knowledge";
      citation: KnowledgeCitation;
    }
  | {
      id: string;
      kind: "toolUi";
      callId: string;
      title?: string;
      toolName?: string;
    };

export type DockTabId = DockTab["id"];

/** Drop retired dock tabs (pre-drawer trajectory) if they appear in stored lists. */
export function ensureNoTrajectoryTab<T extends { kind: string }>(
  tabs: readonly T[],
): T[] {
  return tabs.filter((t) => t.kind !== "trajectory");
}

function loadPanelMode(): PanelMode {
  try {
    const saved = localStorage.getItem(PANEL_MODE_KEY);
    if (saved === "bottom" || saved === "right" || saved === "popup") {
      return saved;
    }
    for (const key of [LEGACY_FILE_MODE_KEY, LEGACY_BROWSER_MODE_KEY]) {
      const legacy = localStorage.getItem(key);
      if (legacy === "bottom" || legacy === "right" || legacy === "popup") {
        return legacy;
      }
    }
  } catch {
    /* ignore */
  }
  return "right";
}

function loadPanelSizes(): PanelSizes {
  try {
    const saved = localStorage.getItem(PANEL_SIZE_KEY);
    if (saved) {
      return JSON.parse(saved) as PanelSizes;
    }
    for (const key of [LEGACY_FILE_SIZE_KEY, LEGACY_BROWSER_SIZE_KEY]) {
      const legacy = localStorage.getItem(key);
      if (legacy) {
        return JSON.parse(legacy) as PanelSizes;
      }
    }
  } catch {
    /* ignore */
  }
  return { rightWidth: 560, bottomHeight: 380 };
}

function persistPanelSizes(sizes: PanelSizes) {
  try {
    localStorage.setItem(PANEL_SIZE_KEY, JSON.stringify(sizes));
  } catch {
    /* ignore */
  }
}

function ensureFilesTab(tabs: DockTab[]): DockTab[] {
  if (tabs.some((t) => t.id === "files")) return tabs;
  return [{ id: "files", kind: "files" }, ...tabs];
}

function fallbackActiveId(
  tabs: DockTab[],
  closedId: DockTabId,
  prevActive: DockTabId | null,
): DockTabId | null {
  if (tabs.length === 0) return null;
  if (prevActive !== closedId && tabs.some((t) => t.id === prevActive)) {
    return prevActive;
  }
  if (tabs.some((t) => t.id === "files")) return "files";
  return tabs[tabs.length - 1]?.id ?? null;
}

interface DockSessionState {
  dockOpen: boolean;
  openTabs: DockTab[];
  activeTabId: DockTabId | null;
}

function defaultDockSessionState(
  threadId: string | null | undefined,
  defaultWorkspaceOpen: boolean,
): DockSessionState {
  // The chat page opts into an immediately visible workspace, including its
  // pending new-chat route before navigation has created a thread.
  if (defaultWorkspaceOpen && threadId !== undefined) {
    return {
      dockOpen: true,
      openTabs: [{ id: "workspace", kind: "workspace" }],
      activeTabId: "workspace",
    };
  }
  return { dockOpen: false, openTabs: [], activeTabId: null };
}

function dockSessionKey(
  agentId: string | null | undefined,
  threadId: string | null | undefined,
): string {
  return `${agentId ?? ""}\0${threadId ?? ""}`;
}

/**
 * Shared chat dock with tabbed file list / file viewers / browser / terminal.
 */
export function useChatDockPanel(
  isMobile: boolean,
  agentId?: string | null,
  threadId?: string | null,
  defaultWorkspaceOpen = false,
) {
  const sessionKey = dockSessionKey(agentId, threadId);
  const [sessionStates, setSessionStates] = useState<
    Record<string, DockSessionState>
  >(() => ({
    [sessionKey]: defaultDockSessionState(threadId, defaultWorkspaceOpen),
  }));
  const [dockMode, setDockMode] = useState<PanelMode>(loadPanelMode);
  const { panelSizes, isResizing, handleResizeStart } = usePanelResize(
    loadPanelSizes(),
    persistPanelSizes,
  );

  const currentSession =
    sessionStates[sessionKey] ??
    defaultDockSessionState(threadId, defaultWorkspaceOpen);
  const { dockOpen, openTabs, activeTabId } = currentSession;

  const updateSession = useCallback(
    (updater: (state: DockSessionState) => DockSessionState) => {
      setSessionStates((prev) => {
        const current =
          prev[sessionKey] ??
          defaultDockSessionState(threadId, defaultWorkspaceOpen);
        return { ...prev, [sessionKey]: updater(current) };
      });
    },
    [defaultWorkspaceOpen, sessionKey, threadId],
  );

  const openDock = useCallback(() => {
    updateSession((state) => ({ ...state, dockOpen: true }));
    if (isMobile) {
      setDockMode("bottom");
    }
  }, [isMobile, setDockMode, updateSession]);

  const handleClose = useCallback(() => {
    updateSession((state) => {
      // Closing the dock restores tool UIs to the message stream.
      const next = state.openTabs.filter((t) => t.kind !== "toolUi");
      const activeTabId =
        state.activeTabId == null ||
        next.some((t) => t.id === state.activeTabId)
          ? state.activeTabId
          : next[0]?.id ?? null;
      return { dockOpen: false, openTabs: next, activeTabId };
    });
  }, [updateSession]);

  const openFileList = useCallback(() => {
    updateSession((state) => ({
      ...state,
      openTabs: ensureFilesTab(state.openTabs),
      activeTabId: "files",
    }));
    openDock();
  }, [openDock, updateSession]);

  const openFileAt = useCallback(
    (path?: string | null, fileAgentId?: string | null) => {
      if (!path?.trim()) {
        openFileList();
        return;
      }
      const ownerId = (fileAgentId || agentId || "").trim() || null;
      const hostAbs = normalizeDockFilePath(path);
      // Keep host-absolute tool paths. Collapsing ``~/.mosswork/agents/<id>/…`` to a
      // relative key breaks virtual ``root_dir`` nests (bytes live under
      // ``{root}/Users/…/.mosswork/agents/<id>/…``, not the real agent home).
      const tabPath = isHostAbsolutePath(hostAbs)
        ? hostAbs
        : canonicalizeDockFilePath(path, ownerId);
      if (!tabPath) {
        openFileList();
        return;
      }
      const id = dockFileTabId(tabPath, ownerId);
      updateSession((state) => {
        if (state.openTabs.some((t) => t.id === id)) {
          return { ...state, activeTabId: id };
        }
        return {
          ...state,
          openTabs: [
            ...state.openTabs,
            {
              id,
              kind: "file" as const,
              path: tabPath,
              ...(ownerId ? { agentId: ownerId } : {}),
            },
          ],
          activeTabId: id,
        };
      });
      openDock();
    },
    [agentId, openDock, openFileList, updateSession],
  );

  const openWorkspaceTab = useCallback(() => {
    updateSession((state) => {
      if (state.openTabs.some((t) => t.id === "workspace")) {
        return { ...state, activeTabId: "workspace" };
      }
      return {
        ...state,
        openTabs: [...state.openTabs, { id: "workspace", kind: "workspace" }],
        activeTabId: "workspace",
      };
    });
    openDock();
  }, [openDock, updateSession]);

  const openBrowserTab = useCallback(() => {
    updateSession((state) => {
      if (state.openTabs.some((t) => t.id === "browser")) {
        return { ...state, activeTabId: "browser" };
      }
      return {
        ...state,
        openTabs: [...state.openTabs, { id: "browser", kind: "browser" }],
        activeTabId: "browser",
      };
    });
    openDock();
  }, [openDock, updateSession]);

  const openTerminalTab = useCallback(() => {
    updateSession((state) => {
      if (state.openTabs.some((t) => t.id === "terminal")) {
        return { ...state, activeTabId: "terminal" };
      }
      return {
        ...state,
        openTabs: [...state.openTabs, { id: "terminal", kind: "terminal" }],
        activeTabId: "terminal",
      };
    });
    openDock();
  }, [openDock, updateSession]);

  const openKnowledgeCitation = useCallback(
    (citation: KnowledgeCitation) => {
      const kbId = citation.kbId?.trim();
      const docId = citation.docId?.trim();
      if (!kbId || !docId) return;
      const id = dockKnowledgeTabId(kbId, docId);
      updateSession((state) => {
        const existing = state.openTabs.find((t) => t.id === id);
        if (existing?.kind === "knowledge") {
          return {
            ...state,
            openTabs: state.openTabs.map((t) =>
              t.id === id ? { ...t, citation } : t,
            ),
            activeTabId: id,
          };
        }
        if (existing) return { ...state, activeTabId: id };
        return {
          ...state,
          openTabs: [
            ...state.openTabs,
            { id, kind: "knowledge" as const, citation },
          ],
          activeTabId: id,
        };
      });
      openDock();
    },
    [openDock, updateSession],
  );

  const openToolUiTab = useCallback(
    (opts: { callId: string; title?: string; toolName?: string }) => {
      const callId = opts.callId?.trim();
      if (!callId) return;
      const id = dockToolUiTabId(callId);
      updateSession((state) => {
        if (state.openTabs.some((t) => t.id === id)) {
          return { ...state, activeTabId: id };
        }
        return {
          ...state,
          openTabs: [
            ...state.openTabs,
            {
              id,
              kind: "toolUi" as const,
              callId,
              title: opts.title,
              toolName: opts.toolName,
            },
          ],
          activeTabId: id,
        };
      });
      openDock();
    },
    [openDock, updateSession],
  );

  const focusToolUiTab = useCallback(
    (callId: string) => {
      const id = dockToolUiTabId(callId);
      updateSession((state) => ({ ...state, activeTabId: id }));
      openDock();
    },
    [openDock, updateSession],
  );

  /** Toggle dock open/closed around a dedicated tab. */
  const toggleDockTab = useCallback(
    (tab: Extract<DockTab, { kind: "browser" | "terminal" | "workspace" }>) => {
      updateSession((state) => {
        if (state.dockOpen && state.activeTabId === tab.id) {
          return { ...state, dockOpen: false };
        }
        return {
          ...state,
          dockOpen: true,
          openTabs: state.openTabs.some((t) => t.id === tab.id)
            ? state.openTabs
            : [...state.openTabs, tab],
          activeTabId: tab.id,
        };
      });
      if (isMobile) {
        setDockMode("bottom");
      }
    },
    [isMobile, updateSession],
  );

  const toggleWorkspacePanel = useCallback(() => {
    toggleDockTab({ id: "workspace", kind: "workspace" });
  }, [toggleDockTab]);

  const toggleBrowserPanel = useCallback(() => {
    toggleDockTab({ id: "browser", kind: "browser" });
  }, [toggleDockTab]);

  const toggleTerminalPanel = useCallback(() => {
    toggleDockTab({ id: "terminal", kind: "terminal" });
  }, [toggleDockTab]);

  const closeTab = useCallback(
    (id: DockTabId) => {
      updateSession((state) => {
        const next = state.openTabs.filter((t) => t.id !== id);
        return {
          dockOpen: next.length > 0 ? state.dockOpen : false,
          openTabs: next,
          activeTabId: fallbackActiveId(next, id, state.activeTabId),
        };
      });
    },
    [updateSession],
  );

  const setActiveTab = useCallback(
    (id: DockTabId) => {
      updateSession((state) => ({
        ...state,
        activeTabId: id,
        dockOpen: true,
      }));
    },
    [updateSession],
  );

  const handleModeChange = useCallback(
    (mode: PanelMode) => {
      if (isMobile && mode === "right") {
        setDockMode("bottom");
        return;
      }
      setDockMode(mode);
    },
    [isMobile],
  );

  useEffect(() => {
    try {
      localStorage.setItem(PANEL_MODE_KEY, dockMode);
    } catch {
      /* ignore */
    }
  }, [dockMode]);

  const activeTab =
    openTabs.find((t) => t.id === activeTabId) ?? openTabs[0] ?? null;
  const filePath = activeTab?.kind === "file" ? activeTab.path : null;

  return {
    dockOpen,
    dockMode,
    filePath,
    openTabs,
    activeTabId,
    activeTab,
    panelSizes,
    isResizing,
    handleResizeStart,
    handleClose,
    handleModeChange,
    openFileAt,
    openFileList,
    openKnowledgeCitation,
    openWorkspaceTab,
    toggleWorkspacePanel,
    openBrowserTab,
    toggleBrowserPanel,
    openTerminalTab,
    toggleTerminalPanel,
    openToolUiTab,
    focusToolUiTab,
    closeTab,
    setActiveTab,
  };
}
