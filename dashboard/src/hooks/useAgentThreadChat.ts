/**
 * Bootstrap a dashboard thread for an agent and wire it to the shared chat store.
 * Used by Terminal AI and other embedded chat surfaces (same path as /chat).
 */
import { useCallback, useEffect, useState } from "react";
import { mossworkThreadsApi } from "../api/modules/mossworkThreads";
import { useChat } from "../pages/Chat/hooks/useChat";
import {
  pickPreferredSession,
  toSession,
} from "../pages/Chat/hooks/useSessions";

export function useAgentThreadChat(
  agentId: string | null,
  freshKey: number | false = false,
) {
  const fresh = freshKey !== false;
  const [binding, setBinding] = useState<{
    agentId: string;
    freshKey: number | false;
    threadId: string;
  } | null>(null);
  const threadId =
    binding?.agentId === agentId && binding?.freshKey === freshKey
      ? binding.threadId
      : null;
  const [booting, setBooting] = useState(false);
  const [bootError, setBootError] = useState<string | null>(null);

  const { messages, isStreaming, sendMessage, cancelStream, loadHistory } =
    useChat(threadId, agentId);

  useEffect(() => {
    if (!agentId) {
      setBinding(null);
      setBootError(null);
      return;
    }

    let cancelled = false;
    setBooting(true);
    setBootError(null);

    void (async () => {
      try {
        const rows = fresh ? [] : await mossworkThreadsApi.list(agentId);
        const preferred = pickPreferredSession(rows.map(toSession));
        let tid = preferred?.threadId;
        if (fresh || !tid) {
          const created = await mossworkThreadsApi.create(agentId);
          tid = created.thread_id;
        }
        await mossworkThreadsApi.rebind(agentId, tid);
        if (!cancelled) {
          setBinding({ agentId, freshKey, threadId: tid });
        }
      } catch (err) {
        if (!cancelled) {
          setBootError(
            err instanceof Error ? err.message : "Failed to load chat thread",
          );
          setBinding(null);
        }
      } finally {
        if (!cancelled) {
          setBooting(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [agentId, freshKey]);

  useEffect(() => {
    if (threadId && agentId) {
      void loadHistory(threadId);
    }
  }, [threadId, agentId, loadHistory]);

  const send = useCallback(
    (text: string) => {
      if (!agentId || !threadId) return;
      const trimmed = text.trim();
      if (!trimmed) return;
      sendMessage(trimmed, "", agentId, undefined, threadId);
    },
    [agentId, threadId, sendMessage],
  );

  return {
    threadId,
    booting,
    bootError,
    messages,
    isStreaming,
    send,
    cancelStream,
  };
}
