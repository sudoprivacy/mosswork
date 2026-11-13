import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { Suspense } from "react";
import fixture from "../../api/fixtures/humanReviewDocument.json";
import {
  humanReviewApi,
  reviewDocumentPath,
  validateReviewDocument,
} from "../../api/modules/humanReview";
import { AUTH_TOKEN_CHANGED_EVENT } from "../../api/request";
import HumanReviewPage, { HumanReviewPanel } from ".";
import { buildReviewFrameDocument } from "./frameReadBridge";

vi.mock("../../api/modules/humanReview", async (original) => ({
  ...(await original<typeof import("../../api/modules/humanReview")>()),
  humanReviewApi: { restore: vi.fn(), capture: vi.fn(), readPage: vi.fn() },
}));
vi.mock("react-i18next", async () => {
  const { default: catalog } = await import("../../locales/en.json");
  return {
    useTranslation: () => ({
      t: (key: string) =>
        catalog.humanReview[
          key.replace("humanReview.", "") as keyof typeof catalog.humanReview
        ] ?? key,
    }),
  };
});

const original = validateReviewDocument(
  fixture.original.target,
  fixture.original.document,
);
const other = validateReviewDocument(
  fixture.other.target,
  fixture.other.document,
);
const restore = vi.mocked(humanReviewApi.restore);
const capture = vi.mocked(humanReviewApi.capture);
const readPage = vi.mocked(humanReviewApi.readPage);
const frame = () => screen.queryByTitle("Read-only model review");
const channelId = "11111111-1111-4111-8111-111111111111";
function presentation(document = original, target = fixture.original.target) {
  return buildReviewFrameDocument(
    document,
    {
      shareId: target.reviewId,
      channel: `${channelId}:${document.record.record_sha256}`,
      endpoint: `${reviewDocumentPath(target)}/page`,
    },
    window.location.origin,
  );
}
function frameRequest(body = { operation: "page" }) {
  const iframe = frame() as HTMLIFrameElement;
  const binding = JSON.parse(
    iframe.getAttribute("srcdoc")!.match(/var binding=(.*?);/)![1],
  );
  window.dispatchEvent(
    new MessageEvent("message", {
      origin: "null",
      source: iframe.contentWindow,
      data: { source: "shareone-host-read", ...binding, id: "1", body },
    }),
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem("auth_token", "original-human");
  restore.mockReset().mockResolvedValue(original);
  capture.mockReset().mockResolvedValue(original);
  readPage.mockReset().mockResolvedValue({ status: 200, body: { result: {} } });
  vi.spyOn(crypto, "randomUUID").mockReturnValue(channelId);
});
afterEach(() => {
  vi.restoreAllMocks();
});

it("restores original saved bytes into the separate SDK presentation document", async () => {
  render(<HumanReviewPanel {...fixture.original.target} />);
  await waitFor(() => expect(frame()).not.toBeNull());
  expect(frame()).toHaveAttribute("srcdoc", presentation());
  expect(original.html).toBe(fixture.original.document.html);
  expect(frame()).toHaveAttribute("sandbox", "allow-scripts");
  expect(frame()).toHaveAttribute("referrerpolicy", "no-referrer");
  expect(frame()).not.toHaveAttribute("src");
  expect(frame()?.outerHTML).not.toContain("original-human");
  expect(capture).not.toHaveBeenCalled();
  expect(
    screen.getByRole("button", { name: "Open and save review" }),
  ).toBeDisabled();
});

it("keeps a missing provider closed and does not expose raw diagnostic data", async () => {
  restore.mockRejectedValue(
    new Error("fixture SQL path and credential must stay private"),
  );
  render(<HumanReviewPanel {...fixture.original.target} />);
  await screen.findByText(
    "This review is unavailable. Check your access and whether review is enabled.",
  );
  expect(screen.queryByText(/SQL path/)).toBeNull();
  expect(frame()).toBeNull();
  expect(capture).not.toHaveBeenCalled();
});

it("opens only on explicit capture, waits for persistence and refuses duplicate submission", async () => {
  restore.mockRejectedValueOnce(new Error("not saved"));
  const saved = deferred<typeof original>();
  capture.mockReturnValue(saved.promise);
  render(<HumanReviewPanel {...fixture.original.target} />);
  await screen.findByText(
    "This review is unavailable. Check your access and whether review is enabled.",
  );
  const button = screen.getByRole("button", { name: "Open and save review" });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(capture).toHaveBeenCalledTimes(1);
  expect(frame()).toBeNull();
  await act(async () => saved.resolve(original));
  expect(frame()).toHaveAttribute("srcdoc", presentation());
});

it("recovers after a lost capture reply using GET only", async () => {
  restore
    .mockRejectedValueOnce(new Error("not saved"))
    .mockResolvedValue(original);
  capture.mockRejectedValue(new Error("lost reply after commit"));
  render(<HumanReviewPanel {...fixture.original.target} />);
  await screen.findByText(
    "This review is unavailable. Check your access and whether review is enabled.",
  );
  fireEvent.click(screen.getByRole("button", { name: "Open and save review" }));
  await screen.findByText(
    "The save result is unconfirmed. Read the saved review to recover the original document.",
  );
  expect(
    screen.getByRole("button", { name: "Open and save review" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Read saved review" }));
  await waitFor(() => expect(frame()).not.toBeNull());
  expect(restore).toHaveBeenCalledTimes(2);
  expect(capture).toHaveBeenCalledTimes(1);
});

it("withdraws visible HTML immediately when the login changes", async () => {
  render(<HumanReviewPanel {...fixture.original.target} />);
  await waitFor(() => expect(frame()).not.toBeNull());
  restore.mockReturnValue(new Promise(() => {}));
  act(() => {
    localStorage.setItem("auth_token", "another-human");
    window.dispatchEvent(new Event(AUTH_TOKEN_CHANGED_EVENT));
  });
  expect(frame()).toBeNull();
  expect(capture).not.toHaveBeenCalled();
});

it("does not release a late reply after sign-out even if the request ignores abort", async () => {
  const late = deferred<typeof original>();
  restore
    .mockReturnValueOnce(late.promise)
    .mockRejectedValue(new Error("signed out"));
  render(<HumanReviewPanel {...fixture.original.target} />);
  act(() => {
    localStorage.clear();
    window.dispatchEvent(new Event(AUTH_TOKEN_CHANGED_EVENT));
  });
  await act(async () => late.resolve(original));
  expect(frame()).toBeNull();
});

it("does not carry a document across projects or release the prior pending reply", async () => {
  const late = deferred<typeof original>();
  const next = deferred<typeof other>();
  restore.mockReturnValueOnce(late.promise).mockReturnValueOnce(next.promise);
  const view = render(<HumanReviewPanel {...fixture.original.target} />);
  view.rerender(<HumanReviewPanel {...fixture.other.target} />);
  await act(async () => late.resolve(original));
  expect(frame()).toBeNull();
  await act(async () => next.resolve(other));
  expect(frame()).toHaveAttribute(
    "srcdoc",
    presentation(other, fixture.other.target),
  );
});

it("removes the prior visible document before a new project read finishes", async () => {
  const view = render(<HumanReviewPanel {...fixture.original.target} />);
  await waitFor(() => expect(frame()).not.toBeNull());
  restore.mockReturnValue(new Promise(() => {}));
  view.rerender(<HumanReviewPanel {...fixture.other.target} />);
  expect(frame()).toBeNull();
});

it("aborts its pending read on unmount", () => {
  restore.mockReturnValue(new Promise(() => {}));
  const view = render(<HumanReviewPanel {...fixture.original.target} />);
  const signal = restore.mock.calls[0][1];
  view.unmount();
  expect(signal?.aborted).toBe(true);
});

it("reads original page parameters without Agent or session selectors", async () => {
  render(
    <MemoryRouter
      initialEntries={[
        `/reviews/${fixture.original.target.projectId}/${fixture.original.target.reviewId}`,
      ]}
    >
      <Suspense fallback={null}>
        <Routes>
          <Route
            path="/reviews/:projectId/:reviewId"
            element={<HumanReviewPage />}
          />
        </Routes>
      </Suspense>
    </MemoryRouter>,
  );
  await waitFor(() => expect(frame()).not.toBeNull());
  expect(restore.mock.calls[0][0]).toEqual(fixture.original.target);
  expect(capture).not.toHaveBeenCalled();
});

it("uses the current original document for SDK page reads and withdraws on denial", async () => {
  render(<HumanReviewPanel {...fixture.original.target} />);
  await waitFor(() => expect(frame()).not.toBeNull());
  readPage.mockRejectedValueOnce(new Error("private provider diagnostic"));
  act(() => frameRequest());
  await waitFor(() => expect(frame()).toBeNull());
  expect(readPage.mock.calls[0].slice(0, 2)).toEqual([
    fixture.original.target,
    original,
  ]);
  expect(capture).not.toHaveBeenCalled();
  expect(screen.queryByText(/private provider/)).toBeNull();
});

it("aborts SDK reads and discards a late reply after auth withdrawal", async () => {
  const result = deferred<{ status: number; body: unknown }>();
  readPage.mockReturnValueOnce(result.promise);
  render(<HumanReviewPanel {...fixture.original.target} />);
  await waitFor(() => expect(frame()).not.toBeNull());
  const post = vi.spyOn(
    (frame() as HTMLIFrameElement).contentWindow!,
    "postMessage",
  );
  act(() => frameRequest());
  const signal = readPage.mock.calls[0][3];
  restore.mockReturnValueOnce(new Promise(() => {}));
  act(() => {
    localStorage.clear();
    window.dispatchEvent(new Event(AUTH_TOKEN_CHANGED_EVENT));
  });
  expect(frame()).toBeNull();
  expect(signal?.aborted).toBe(true);
  await act(async () => result.resolve({ status: 200, body: { result: {} } }));
  expect(post).not.toHaveBeenCalled();
});
