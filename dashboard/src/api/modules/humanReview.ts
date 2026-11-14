import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { getAuthToken, request } from "../request";

export interface ReviewTarget {
  projectId: string;
  reviewId: string;
}

export interface ServedReviewRecord {
  readonly format: "sudograph-served-review/v1";
  readonly authority_ref: string;
  readonly model_ref: string;
  readonly resource_ref: string;
  readonly source_refs: readonly string[];
  readonly revision: string;
  readonly context_digest: string;
  readonly endpoint: string;
  readonly html_sha256: string;
  readonly record_sha256: string;
}

export interface ServedReviewDocument {
  readonly record: ServedReviewRecord;
  readonly html: string;
}

export class ReviewDocumentError extends Error {}

const reference = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/;
const resource = /^[a-zA-Z0-9_-]{1,128}$/;
const digest = /^[a-f0-9]{64}$/;
const matches = (pattern: RegExp, value: unknown): value is string =>
  typeof value === "string" && pattern.exec(value)?.[0] === value;
const fields = [
  "format",
  "authority_ref",
  "model_ref",
  "resource_ref",
  "source_refs",
  "revision",
  "context_digest",
  "endpoint",
  "html_sha256",
  "record_sha256",
].sort();

export function reviewDocumentPath(target: ReviewTarget): string {
  if (
    !matches(resource, target.projectId) ||
    !matches(/^[a-f0-9]{32}$/, target.reviewId)
  ) {
    throw new ReviewDocumentError("humanReview.invalidLink");
  }
  return `/identity/ontology-projects/${target.projectId}/reviews/${target.reviewId}/v1`;
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

const hash = (value: string) =>
  bytesToHex(sha256(new TextEncoder().encode(value)));

/** Check consistency of the original stored bytes; server grants remain mandatory. */
export function validateReviewDocument(
  target: ReviewTarget,
  value: unknown,
): ServedReviewDocument {
  reviewDocumentPath(target);
  const fail = () => {
    throw new ReviewDocumentError("humanReview.documentMismatch");
  };
  if (!object(value) || Object.keys(value).sort().join(",") !== "html,record")
    return fail();
  const { html, record } = value;
  if (
    !object(record) ||
    Object.keys(record).sort().join(",") !== fields.join(",")
  )
    return fail();
  if (
    typeof html !== "string" ||
    !html.trim() ||
    record.format !== "sudograph-served-review/v1" ||
    !matches(reference, record.authority_ref) ||
    !matches(resource, record.model_ref) ||
    record.resource_ref !== target.projectId ||
    record.endpoint !== `/review/${target.reviewId}/v1` ||
    ![
      record.revision,
      record.context_digest,
      record.html_sha256,
      record.record_sha256,
    ].every((item) => matches(digest, item)) ||
    !Array.isArray(record.source_refs) ||
    record.source_refs.length > 1000 ||
    !record.source_refs.every((item) => matches(reference, item))
  )
    return fail();
  const sources = record.source_refs as string[];
  if (sources.some((item, index) => index > 0 && sources[index - 1] >= item))
    return fail();
  const metadata = Object.fromEntries(
    fields
      .filter((key) => key !== "record_sha256")
      .map((key) => [key, record[key]]),
  );
  if (
    hash(html) !== record.html_sha256 ||
    hash(JSON.stringify(metadata)) !== record.record_sha256
  )
    return fail();
  return Object.freeze({
    html,
    record: Object.freeze({
      ...record,
      source_refs: Object.freeze([...sources]),
    }) as unknown as ServedReviewRecord,
  });
}

async function load(
  target: ReviewTarget,
  method: "GET" | "POST",
  signal?: AbortSignal,
) {
  const path = reviewDocumentPath(target);
  const token = getAuthToken();
  if (!token) throw new ReviewDocumentError("humanReview.signIn");
  const value = await request<unknown>(path, {
    method,
    signal,
    cache: "no-store",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (signal?.aborted || token !== getAuthToken()) {
    throw new ReviewDocumentError("humanReview.authChanged");
  }
  return validateReviewDocument(target, value);
}

export const humanReviewApi = {
  restore: (target: ReviewTarget, signal?: AbortSignal) =>
    load(target, "GET", signal),
  capture: (target: ReviewTarget, signal?: AbortSignal) =>
    load(target, "POST", signal),
  readPage: async (
    target: ReviewTarget,
    document: ServedReviewDocument,
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<{ status: number; body: unknown }> => {
    const path = `${reviewDocumentPath(target)}/page`;
    validateReviewDocument(target, document);
    // This is consistency with the displayed record, never a source grant or
    // a second paging codec. The server restores and authorizes original data.
    if (
      Object.keys(body).sort().join(",") !== "operation,params,view" ||
      body.operation !== "page" ||
      body.view !== document.record.resource_ref ||
      !object(body.params) ||
      body.params.revision !== document.record.revision ||
      body.params.context_digest !== document.record.context_digest
    )
      throw new ReviewDocumentError("humanReview.documentMismatch");
    const serialized = JSON.stringify(body);
    if (new TextEncoder().encode(serialized).byteLength > 65536)
      throw new ReviewDocumentError("humanReview.documentMismatch");
    const token = getAuthToken();
    if (!token) throw new ReviewDocumentError("humanReview.signIn");
    const result = await request<unknown>(path, {
      method: "POST",
      cache: "no-store",
      redirect: "error",
      signal,
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "If-Match": `"${document.record.record_sha256}"`,
      },
      body: serialized,
    });
    if (signal?.aborted || token !== getAuthToken())
      throw new ReviewDocumentError("humanReview.authChanged");
    if (
      !object(result) ||
      Object.keys(result).join(",") !== "result" ||
      !object(result.result) ||
      new TextEncoder().encode(JSON.stringify(result)).byteLength >
        32 * 1024 * 1024
    )
      throw new ReviewDocumentError("humanReview.documentMismatch");
    return { status: 200, body: result };
  },
};
