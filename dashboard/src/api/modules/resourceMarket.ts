import { request, requestBlob } from "../request";

export type MarketManifest = {
  kind: "skill" | "expert";
  slug: string;
  name: string;
  description: string;
  version: string;
  changelog: string;
  dependencies: { resource_id: string; version: string }[];
  runtime: {
    execution_targets: string[];
    platforms: string[];
    python: string;
    runtime_profile: string;
  };
  configuration: Record<
    string,
    { type: string; required: boolean; description: string }
  >;
  license: string;
  author: string;
};

export type MarketResource = {
  resource_id: string;
  slug: string;
  kind: "skill" | "expert";
  name: string;
  description: string;
  owner_user_id: number | null;
  listed: boolean;
  recommended_version_id: string | null;
  recommended_version: string | null;
  version_count: number;
  icon_url?: string | null;
};

export type MarketVersion = {
  version_id: string;
  resource_id: string;
  version: string;
  object_sha256: string;
  manifest: MarketManifest;
  published_at: number;
};

export type MarketDraft = {
  draft_id: string;
  resource_id: string;
  version: string;
  state: "draft" | "submitted" | "rejected" | "published";
  object_sha256: string;
  manifest: MarketManifest;
  files: { path: string; size: number; sha256: string }[];
  file_count: number;
  total_bytes: number;
  review_note: string;
};

export type MarketDetail = {
  resource: MarketResource;
  versions: MarketVersion[];
  drafts: MarketDraft[];
};

export const resourceMarketApi = {
  list: (offset = 0) =>
    request<MarketResource[]>(`/market/resources?offset=${offset}`),
  detail: (resourceId: string) =>
    request<MarketDetail>(
      `/market/resources/${encodeURIComponent(resourceId)}`,
    ),
  import: (file: File, resourceId?: string) =>
    request<MarketDraft>(
      `/market/import${
        resourceId ? `?resource_id=${encodeURIComponent(resourceId)}` : ""
      }`,
      {
        method: "POST",
        headers: { "Content-Type": "application/zip" },
        body: file,
      },
    ),
  submit: (draft: MarketDraft) =>
    request<MarketDraft>(`/market/drafts/${draft.draft_id}/submit`, {
      method: "POST",
      body: JSON.stringify({ expected_sha256: draft.object_sha256 }),
    }),
  review: (draft: MarketDraft, action: "publish" | "reject", note: string) =>
    request<MarketDraft>(`/market/drafts/${draft.draft_id}/review`, {
      method: "POST",
      body: JSON.stringify({
        expected_sha256: draft.object_sha256,
        action,
        note,
      }),
    }),
  preview: (draftId: string, path: string) =>
    request<{ content: string }>(
      `/market/drafts/${draftId}/preview?path=${encodeURIComponent(path)}`,
    ),
  listing: (resourceId: string, listed: boolean) =>
    request<void>(`/market/resources/${resourceId}/listing`, {
      method: "PUT",
      body: JSON.stringify({ listed }),
    }),
  recommend: (resource: MarketResource, versionId: string) =>
    request<void>(`/market/resources/${resource.resource_id}/recommendation`, {
      method: "PUT",
      body: JSON.stringify({
        version_id: versionId,
        expected_version_id: resource.recommended_version_id,
      }),
    }),
  download: (versionId: string) =>
    requestBlob(`/market/versions/${versionId}/download`),
  example: (kind: "skill" | "expert") =>
    requestBlob(`/market/examples/${kind}`),
};
