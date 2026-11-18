import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  create: vi.fn(),
  rebind: vi.fn(),
  loadHistory: vi.fn(),
}));
vi.mock("../api/modules/mossworkThreads", () => ({
  mossworkThreadsApi: { create: mocks.create, rebind: mocks.rebind },
}));
vi.mock("../pages/Chat/hooks/useChat", () => ({
  useChat: (threadId: string | null) => ({
    messages: threadId ? [{ id: threadId }] : [],
    isStreaming: false,
    sendMessage: vi.fn(),
    cancelStream: vi.fn(),
    loadHistory: mocks.loadHistory,
  }),
}));
import { useAgentThreadChat } from "./useAgentThreadChat";

beforeEach(() => vi.clearAllMocks());

it("hides old messages immediately on every test reset and after closing", async () => {
  mocks.create.mockResolvedValueOnce({ thread_id: "first" });
  const { result, rerender } = renderHook(
    ({ agentId, reset }: { agentId: string | null; reset: number }) =>
      useAgentThreadChat(agentId, reset),
    { initialProps: { agentId: "expert" as string | null, reset: 1 } },
  );
  await waitFor(() => expect(result.current.threadId).toBe("first"));

  for (const reset of [2, 3]) {
    let resolve!: (value: { thread_id: string }) => void;
    mocks.create.mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    rerender({ agentId: "expert", reset });
    expect(result.current.messages).toEqual([]);
    expect(result.current.threadId).toBeNull();
    await act(async () => resolve({ thread_id: `test-${reset}` }));
    await waitFor(() => expect(result.current.threadId).toBe(`test-${reset}`));
  }
  rerender({ agentId: null, reset: 3 });
  expect(result.current.messages).toEqual([]);
});
