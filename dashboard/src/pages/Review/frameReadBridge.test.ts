import { afterEach, expect, it, vi } from "vitest";
import {
  ReviewFrameReads,
  buildReviewFrameDocument,
  type FrameReadReply,
} from "./frameReadBridge";
import { buildHostReadBridgeScript } from "./shareone/host-read-script";
import fixture from "../../api/fixtures/humanReviewDocument.json";
import { validateReviewDocument } from "../../api/modules/humanReview";

const binding = {
  shareId: "original-review",
  channel: "original-channel",
  endpoint: "/original/page",
};
const document = validateReviewDocument(
  fixture.original.target,
  fixture.original.document,
);
const frames: HTMLIFrameElement[] = [];
const bridges: ReviewFrameReads[] = [];
afterEach(() => {
  for (const bridge of bridges.splice(0)) bridge.dispose();
  for (const frame of frames.splice(0)) frame.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});
type Read = (
  request: Record<string, unknown>,
  signal: AbortSignal,
) => Promise<FrameReadReply>;
function setup(
  read: ReturnType<typeof vi.fn<Read>> = vi.fn<Read>(async () => ({
    status: 200,
    body: { result: { rows: [1] } },
  })),
) {
  const frame = window.document.createElement("iframe");
  window.document.body.append(frame);
  frames.push(frame);
  const withdraw = vi.fn();
  const state = { current: true, frame };
  const bridge = new ReviewFrameReads(
    binding,
    () => state.frame,
    () => state.current,
    read,
    withdraw,
  );
  bridges.push(bridge);
  const post = vi.spyOn(frame.contentWindow!, "postMessage");
  const send = (
    change: Record<string, unknown> = {},
    source: MessageEventSource | null = frame.contentWindow,
  ) => {
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "null",
        source,
        data: {
          source: "shareone-host-read",
          ...binding,
          id: "1",
          body: { operation: "page" },
          ...change,
        },
      }),
    );
  };
  return { bridge, frame, post, read, withdraw, state, send };
}

it("keeps original HTML bytes and installs only the original read SDK before compiler scripts", () => {
  const original = {
    ...document,
    html: "<!doctype html><html><head><script>original()</script></head><body>原文</body></html>",
  };
  const source = buildHostReadBridgeScript(binding, window.location.origin);
  const result = buildReviewFrameDocument(
    original,
    binding,
    window.location.origin,
  );
  expect(result.replace("\n" + source, "")).toBe(original.html);
  expect(result.indexOf("sdk.read=")).toBeLessThan(
    result.indexOf("original()"),
  );
  expect(source).not.toContain(binding.endpoint);
  expect(source).not.toContain("Authorization");
  expect(source).not.toContain("anchors");
});

it.each([
  { channel: "obsolete" },
  { shareId: "foreign" },
  { source: "other" },
  { id: "../1" },
  { body: [] },
])("refuses a foreign or malformed original SDK message %j", async (change) => {
  const state = setup();
  state.send(change);
  await Promise.resolve();
  expect(state.read).not.toHaveBeenCalled();
  expect(state.post).not.toHaveBeenCalled();
});

it("requires the exact live frame and snapshots the original body before waits", async () => {
  const state = setup();
  state.send({}, window);
  expect(state.read).not.toHaveBeenCalled();
  const body = { operation: "page", params: { id: "original" } };
  state.send({ body });
  body.params.id = "mutated";
  await Promise.resolve();
  expect(state.read.mock.calls[0][0]).toEqual({
    operation: "page",
    params: { id: "original" },
  });
  expect(state.post).toHaveBeenCalledWith(
    {
      source: "shareone-host-read-result",
      shareId: binding.shareId,
      channel: binding.channel,
      id: "1",
      result: { status: 200, body: { result: { rows: [1] } } },
    },
    "*",
  );
});

it("aborts and never releases a late ignored-abort result from a disposed document", async () => {
  let finish!: (value: { status: number; body: unknown }) => void;
  const read = vi.fn(
    (_body: Record<string, unknown>, _signal: AbortSignal) =>
      new Promise<{ status: number; body: unknown }>((resolve) => {
        finish = resolve;
      }),
  );
  const state = setup(read);
  state.send();
  state.bridge.dispose();
  expect(read.mock.calls[0][1].aborted).toBe(true);
  finish({ status: 200, body: { result: {} } });
  await Promise.resolve();
  expect(state.post).not.toHaveBeenCalled();
  expect(state.withdraw).not.toHaveBeenCalled();
});

it("discards data when current principal or exact frame changed during I/O", async () => {
  let finish!: (value: { status: number; body: unknown }) => void;
  const read = vi.fn(
    () =>
      new Promise<{ status: number; body: unknown }>((resolve) => {
        finish = resolve;
      }),
  );
  const state = setup(read);
  state.send();
  state.state.current = false;
  finish({ status: 200, body: { result: {} } });
  await Promise.resolve();
  expect(state.post).not.toHaveBeenCalled();
});

it("withdraws the current private frame after a failed authorized read", async () => {
  const state = setup(
    vi.fn(async () => {
      throw new Error("private credential diagnostic");
    }),
  );
  state.send();
  await Promise.resolve();
  expect(state.withdraw).toHaveBeenCalledOnce();
  expect(state.post).not.toHaveBeenCalled();
});

it("bounds concurrent reads and request bytes using the existing protocol", async () => {
  const read = vi.fn(
    () => new Promise<{ status: number; body: unknown }>(() => {}),
  );
  const state = setup(read);
  for (let id = 1; id <= 9; id++) state.send({ id: String(id) });
  expect(read).toHaveBeenCalledTimes(8);
  expect(state.post.mock.calls[0][0].result.status).toBe(429);
  state.bridge.dispose();
  const next = setup();
  next.send({ body: { oversized: "x".repeat(65537) } });
  expect(next.read).not.toHaveBeenCalled();
  expect(next.post.mock.calls[0][0].result.status).toBe(429);
});
