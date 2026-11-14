import { afterEach, beforeEach, expect, it, vi } from "vitest";
import fixture from "./fixtures/humanReviewDocument.json";
import { clearSetupRequired, setActiveAgentId } from "./request";
import {
  humanReviewApi,
  reviewDocumentPath,
  validateReviewDocument,
} from "./modules/humanReview";

vi.mock("./config", () => ({ getApiUrl: (path: string) => `/api${path}` }));
vi.mock("../i18n", () => ({ default: { language: "en" } }));

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  clearSetupRequired();
  localStorage.setItem("auth_token", "original-human-access");
  setActiveAgentId("unrelated-agent");
});
afterEach(() => {
  setActiveAgentId(null);
  vi.unstubAllGlobals();
});
const reply = (value: unknown = fixture.original.document) =>
  new Response(JSON.stringify(value), {
    headers: { "content-type": "application/json" },
  });

it("accepts the exact Unicode HTML and metadata produced by original Python ReviewDocument.record", () => {
  const document = validateReviewDocument(
    fixture.original.target,
    fixture.original.document,
  );
  expect(document.html).toBe(fixture.original.document.html);
  expect(document.record).toEqual(fixture.original.document.record);
  expect(Object.isFrozen(document.record.source_refs)).toBe(true);
  expect(Object.isFrozen(document.record)).toBe(true);
});

it.each([
  { projectId: "../foreign", reviewId: "1".repeat(32) },
  { projectId: "original/child", reviewId: "1".repeat(32) },
  { projectId: "", reviewId: "1".repeat(32) },
  { projectId: "project-original", reviewId: "A".repeat(32) },
  { projectId: "project-original", reviewId: "1".repeat(31) },
  { projectId: "project-original\n", reviewId: "1".repeat(32) },
  { projectId: "project-original", reviewId: "1".repeat(32) + "\n" },
])(
  "refuses an invalid route selector before network dispatch: %j",
  async (target) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(humanReviewApi.restore(target)).rejects.toThrow(
      "humanReview.invalidLink",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  },
);

it.each([
  [
    "html",
    (value: typeof fixture.original.document) => {
      value.html += "altered";
    },
  ],
  [
    "record checksum",
    (value: typeof fixture.original.document) => {
      value.record.record_sha256 = "0".repeat(64);
    },
  ],
  [
    "revision",
    (value: typeof fixture.original.document) => {
      value.record.revision = "c".repeat(64);
    },
  ],
  [
    "project",
    (value: typeof fixture.original.document) => {
      value.record.resource_ref = "foreign-project";
    },
  ],
  [
    "endpoint",
    (value: typeof fixture.original.document) => {
      value.record.endpoint = fixture.other.document.record.endpoint;
    },
  ],
  [
    "extra source",
    (value: typeof fixture.original.document) => {
      value.record.source_refs.push("source:unreviewed");
    },
  ],
  [
    "duplicate source",
    (value: typeof fixture.original.document) => {
      value.record.source_refs.push(value.record.source_refs[0]);
    },
  ],
])("withholds inconsistent %s", (_label, change) => {
  const value = structuredClone(fixture.original.document);
  change(value);
  expect(() => validateReviewDocument(fixture.original.target, value)).toThrow(
    "humanReview.documentMismatch",
  );
});

it.each([
  null,
  {},
  { ...fixture.original.document, selected_owner: "other" },
  {
    ...fixture.original.document,
    record: { ...fixture.original.document.record, grant: true },
  },
])("refuses a missing or extended response envelope", (value) => {
  expect(() => validateReviewDocument(fixture.original.target, value)).toThrow(
    "humanReview.documentMismatch",
  );
});

it.each(["restore", "capture"] as const)(
  "uses one authenticated %s with exact original IDs and no Agent metadata",
  async (mode) => {
    const fetchMock = vi.fn(async () => reply());
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    await humanReviewApi[mode](fixture.original.target, controller.signal);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, options] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe(`/api${reviewDocumentPath(fixture.original.target)}`);
    expect(options.method).toBe(mode === "restore" ? "GET" : "POST");
    expect(options.cache).toBe("no-store");
    expect(options.body).toBeUndefined();
    expect(options.signal).toBe(controller.signal);
    expect(new Headers(options.headers).get("Authorization")).toBe(
      "Bearer original-human-access",
    );
    expect(new Headers(options.headers).get("X-mosswork-Agent-Id")).toBeNull();
    expect(url).not.toContain("original-human-access");
  },
);

it("does not fetch without the current login", async () => {
  localStorage.clear();
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  await expect(humanReviewApi.restore(fixture.original.target)).rejects.toThrow(
    "humanReview.signIn",
  );
  expect(fetchMock).not.toHaveBeenCalled();
});

it("withdraws a late response after the authenticated user changes", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      localStorage.setItem("auth_token", "different-human-access");
      return reply();
    }),
  );
  await expect(humanReviewApi.restore(fixture.original.target)).rejects.toThrow(
    "humanReview.authChanged",
  );
});

it("withdraws an aborted response even if the transport finishes", async () => {
  const controller = new AbortController();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      controller.abort();
      return reply();
    }),
  );
  await expect(
    humanReviewApi.restore(fixture.original.target, controller.signal),
  ).rejects.toThrow("humanReview.authChanged");
});

it("never converts failed restore into a capture", async () => {
  const fetchMock = vi.fn(
    async () => new Response("Unavailable", { status: 503 }),
  );
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    humanReviewApi.restore(fixture.original.target),
  ).rejects.toThrow();
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(
    (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].method,
  ).toBe("GET");
});

it("does not replay a capture after a malformed non-error response", async () => {
  let effects = 0;
  const fetchMock = vi.fn(async () => {
    effects++;
    return reply({ html: "unbound" });
  });
  vi.stubGlobal("fetch", fetchMock);
  await expect(humanReviewApi.capture(fixture.original.target)).rejects.toThrow(
    "humanReview.documentMismatch",
  );
  expect(effects).toBe(1);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("does not replay a capture after a lost reply", async () => {
  const fetchMock = vi.fn(async () => {
    throw new TypeError("fixture connection lost");
  });
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    humanReviewApi.capture(fixture.original.target),
  ).rejects.toThrow();
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

function pageRequest() {
  return {
    view: fixture.original.document.record.resource_ref,
    operation: "page",
    params: {
      revision: fixture.original.document.record.revision,
      context_digest: fixture.original.document.record.context_digest,
      id: "sg-source/original/object/widgets",
      offset: 0,
      limit: 50,
    },
  };
}

it("posts the original page request under parent auth and captured If-Match only", async () => {
  const document = validateReviewDocument(
    fixture.original.target,
    fixture.original.document,
  );
  const body = pageRequest();
  const result = { result: { rows: ["original"] } };
  const fetchMock = vi.fn(async () => reply(result));
  vi.stubGlobal("fetch", fetchMock);
  const signal = new AbortController().signal;
  expect(
    await humanReviewApi.readPage(
      fixture.original.target,
      document,
      body,
      signal,
    ),
  ).toEqual({ status: 200, body: result });
  const [url, options] = fetchMock.mock.calls[0] as unknown as [
    string,
    RequestInit,
  ];
  expect(url).toBe(`/api${reviewDocumentPath(fixture.original.target)}/page`);
  expect(options.method).toBe("POST");
  expect(options.redirect).toBe("error");
  expect(options.cache).toBe("no-store");
  expect(options.signal).toBe(signal);
  expect(JSON.parse(options.body as string)).toEqual(body);
  expect(new Headers(options.headers).get("If-Match")).toBe(
    `"${document.record.record_sha256}"`,
  );
  expect(new Headers(options.headers).get("Authorization")).toBe(
    "Bearer original-human-access",
  );
  expect(new Headers(options.headers).get("X-mosswork-Agent-Id")).toBeNull();
});

it.each(["view", "revision", "operation", "endpoint"])(
  "withholds a changed page %s before credentials/network",
  async (field) => {
    const body: Record<string, unknown> = pageRequest();
    if (field === "revision")
      (body.params as Record<string, unknown>).revision = "0".repeat(64);
    else body[field] = "unbound";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      humanReviewApi.readPage(
        fixture.original.target,
        validateReviewDocument(
          fixture.original.target,
          fixture.original.document,
        ),
        body,
      ),
    ).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  },
);

it("discards a page response after token withdrawal and never replays it", async () => {
  const fetchMock = vi.fn(async () => {
    localStorage.clear();
    return reply({ result: { rows: ["withheld"] } });
  });
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    humanReviewApi.readPage(
      fixture.original.target,
      validateReviewDocument(
        fixture.original.target,
        fixture.original.document,
      ),
      pageRequest(),
    ),
  ).rejects.toThrow("humanReview.authChanged");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
