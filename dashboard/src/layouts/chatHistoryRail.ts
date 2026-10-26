export const CHAT_HISTORY_RAIL_ID = "mosswork-chat-history-rail";

/** Open mobile/desktop nav on the minimal-mode records pane. */
export const OPEN_NAV_RECORDS_EVENT = "mosswork:open-nav-records";

export function isChatPath(pathname: string): boolean {
  return pathname === "/chat" || pathname.startsWith("/chat/");
}

export { isGroupedNavKey as isSettingsPaneNavKey } from "./sidebarNav";
