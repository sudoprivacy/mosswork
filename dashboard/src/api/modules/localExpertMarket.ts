import { request, requestBlob } from "../request";
import type { ExpertMarketQuickPrompt } from "./expertMarket";

export type LocalMarketExpert = {
  resource_id: string;
  version_id: string;
  version: string;
  object_sha256: string;
  name: string;
  description: string;
  dependencies: { resource_id: string; version: string }[];
  icon_url?: string | null;
};

export type LocalExpertDetail = LocalMarketExpert & {
  lock: {
    resource_id: string;
    version: string;
    version_id: string;
    object_sha256: string;
  }[];
  file_contents: { name: string; content: string }[];
  welcome_message?: { zh?: string; en?: string } | string | null;
  quick_prompts?: ExpertMarketQuickPrompt[];
};

export const localExpertMarketApi = {
  list: () =>
    request<{ items: LocalMarketExpert[]; can_create: boolean }>(
      "/experts/market",
    ),
  detail: (versionId: string) =>
    request<LocalExpertDetail>(
      `/experts/market/${encodeURIComponent(versionId)}`,
    ),
  icon: (versionId: string) =>
    requestBlob(`/experts/market/${encodeURIComponent(versionId)}/icon`),
  install: (versionId: string, payload: object) =>
    request<{ agent_id: string; name: string }>(
      `/experts/market/${encodeURIComponent(versionId)}/install`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
};
