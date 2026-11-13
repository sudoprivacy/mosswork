import { request, requestBlob } from "../request";

export type LocalSkillRelease = {
  resource_id: string;
  version_id: string;
  version: string;
  object_sha256: string;
  name: string;
  description: string;
  dependencies: { resource_id: string; version: string }[];
  icon_url?: string | null;
};
export type LocalSkillSource = LocalSkillRelease & {
  lock: {
    resource_id: string;
    version: string;
    version_id: string;
    object_sha256: string;
  }[];
};
export type LocalSkillDetail = LocalSkillSource & {
  slug: string;
  changelog: string;
  versions: LocalSkillRelease[];
  file_contents: { name: string; content: string }[];
  runtime: Record<string, unknown>;
  configuration: Record<string, unknown>;
};
export type LocalSkillCatalog = {
  items: LocalSkillRelease[];
  next_offset?: number | null;
};
export type LocalSkillInstallation = {
  package: { id: string; name: string; skill_count: number };
  source: LocalSkillSource;
};
export const localSkillMarketApi = {
  list: (offset = 0, limit = 60) =>
    request<LocalSkillCatalog>(
      `/skill-packages/market?offset=${offset}&limit=${limit}`,
    ),
  detail: (versionId: string) =>
    request<LocalSkillDetail>(
      `/skill-packages/market/${encodeURIComponent(versionId)}`,
    ),
  icon: (versionId: string) =>
    requestBlob(`/skill-packages/market/${encodeURIComponent(versionId)}/icon`),
  install: (versionId: string, name: string) =>
    request<LocalSkillInstallation>(
      `/skill-packages/market/${encodeURIComponent(versionId)}/install`,
      { method: "POST", body: JSON.stringify({ name }) },
    ),
};
