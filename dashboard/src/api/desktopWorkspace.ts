import type { DirectoryGrant } from "./modules/devices";

export interface DesktopWorkspaceState {
  state: "unpaired" | "connecting" | "online" | "reconnecting" | "disconnected";
  device_id: string;
  owner_user_id: number;
  path: string;
  grant: DirectoryGrant | null;
  error?: string;
  renew_at?: number;
  expires_at?: number;
  pair_blocked?: boolean;
}

export interface DesktopWorkspaceBridge {
  status: () => Promise<DesktopWorkspaceState>;
  pair: (code: string) => Promise<DesktopWorkspaceState>;
  renew?: (ticket: string) => Promise<DesktopWorkspaceState>;
  choose: () => Promise<DesktopWorkspaceState>;
  clear: () => Promise<DesktopWorkspaceState>;
  disconnect: () => Promise<DesktopWorkspaceState>;
  showAbout?: () => Promise<DesktopWorkspaceState>;
}

declare global {
  interface Window {
    mossworkDesktopWorkspace?: DesktopWorkspaceBridge;
  }
}

export const DESKTOP_WORKSPACE_READY = "mosswork:desktop-workspace-ready";
export const OPEN_WORKSPACE_SETTINGS = "mosswork:open-workspace-settings";

export function openWorkspaceSettings() {
  window.dispatchEvent(new Event(OPEN_WORKSPACE_SETTINGS));
}
