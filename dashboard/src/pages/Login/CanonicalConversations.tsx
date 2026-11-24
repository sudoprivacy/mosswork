import { Alert, Button, Input, Space, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { buildDashboardChatWsUrl } from "../../api/modules/wsChat";
import { request } from "../../api/request";

interface Conversation {
  agent_id: string;
  thread_id: string;
  title: string;
}

interface Turn {
  question: string;
  answer: string;
  state?: string;
}

export function CanonicalConversations() {
  const { t } = useTranslation();
  const [items, setItems] = useState<Conversation[]>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<Conversation>();
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(false);
    void request<Conversation[]>("/identity/conversations")
      .then((result) => {
        if (!Array.isArray(result))
          throw new Error("Unexpected conversation list");
        if (active) {
          setItems(result);
          if (result.length === 1) setSelected(result[0]);
        }
      })
      .catch(() => {
        if (active) setError(true);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [attempt]);
  if (selected)
    return (
      <CurrentConversation
        key={selected.thread_id}
        item={selected}
        back={() => setSelected(undefined)}
      />
    );
  return (
    <Space direction="vertical">
      <Button
        loading={loading}
        onClick={() => setAttempt((value) => value + 1)}
      >
        {t("canonical.openConversations")}
      </Button>
      {error && (
        <Alert type="warning" message={t("canonical.runtimePending")} />
      )}
      {items?.length === 0 && !error && (
        <Typography.Text>{t("canonical.noConversations")}</Typography.Text>
      )}
      {!error &&
        items?.map((item) => (
          <Button key={item.thread_id} onClick={() => setSelected(item)}>
            {item.title}
          </Button>
        ))}
    </Space>
  );
}

function CurrentConversation({
  item,
  back,
}: {
  item: Conversation;
  back: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [phase, setPhase] = useState<
    "loading" | "ready" | "working" | "unknown" | "historyError"
  >("loading");
  const [older, setOlder] = useState<string | null>(null);
  const [historyUnsupported, setHistoryUnsupported] = useState(false);
  const [history, loadHistory] = useState<{ before: string | null }>({
    before: null,
  });
  const socket = useRef<WebSocket>();
  const observed = useRef<{ run: string; seq: number; index: number }>();
  useEffect(
    () => () => {
      const previous = socket.current;
      socket.current = undefined;
      previous?.close();
    },
    [],
  );
  const connect = useCallback(
    (text?: string) => {
      const checkpoint = observed.current;
      if (socket.current || (text === undefined && !checkpoint)) return;
      const ws = new WebSocket(buildDashboardChatWsUrl(item.agent_id));
      socket.current = ws;
      setPhase("working");
      ws.onopen = () => {
        if (socket.current !== ws) return;
        if (text !== undefined) {
          observed.current = undefined;
          setTurns((previous) => [...previous, { question: text, answer: "" }]);
          setDraft("");
          ws.send(
            JSON.stringify({
              type: "user_turn",
              thread_id: item.thread_id,
              text,
              client_request_id: crypto.randomUUID(),
            }),
          );
        } else if (checkpoint) {
          ws.send(
            JSON.stringify({
              type: "subscribe",
              thread_id: item.thread_id,
              execution_id: checkpoint.run,
              event_seq: checkpoint.seq,
              event_index: checkpoint.index,
            }),
          );
        }
      };
      ws.onmessage = (event) => {
        if (socket.current !== ws) return;
        try {
          const frame = JSON.parse(event.data);
          if (frame.type === "error") {
            ws.close();
            return;
          }
          if (
            frame.thread_id !== item.thread_id ||
            typeof frame.execution_id !== "string" ||
            !Number.isSafeInteger(frame.event_seq) ||
            !Number.isSafeInteger(frame.event_index)
          )
            return;
          const old = observed.current;
          if (
            old &&
            (old.run !== frame.execution_id ||
              old.seq > frame.event_seq ||
              (old.seq === frame.event_seq && old.index >= frame.event_index))
          )
            return;
          observed.current = {
            run: frame.execution_id,
            seq: frame.event_seq,
            index: frame.event_index,
          };
          if (frame.type === "token" && typeof frame.content === "string") {
            setTurns((previous) =>
              previous.map((turn, i) =>
                i === previous.length - 1
                  ? { ...turn, answer: turn.answer + frame.content }
                  : turn,
              ),
            );
          }
          if (frame.type === "done") {
            socket.current = undefined;
            observed.current = undefined;
            setTurns((previous) =>
              previous.map((turn, i) =>
                i === previous.length - 1
                  ? { ...turn, state: frame.execution_state || "completed" }
                  : turn,
              ),
            );
            setPhase(
              frame.execution_state && frame.execution_state !== "completed"
                ? "unknown"
                : "ready",
            );
            ws.close();
          }
        } catch {
          ws.close();
        }
      };
      ws.onclose = (event) => {
        if (socket.current !== ws) return;
        socket.current = undefined;
        if (event.code === 4001 || event.code === 4003) {
          setTurns([]);
          setOlder(null);
          observed.current = undefined;
          setPhase("historyError");
          return;
        }
        setPhase("unknown");
      };
    },
    [item.agent_id, item.thread_id],
  );
  useEffect(() => {
    const ws = new WebSocket(buildDashboardChatWsUrl(item.agent_id));
    socket.current = ws;
    setPhase("loading");
    setHistoryUnsupported(false);
    const page: (Turn & { id: string; seq: number; state: string })[] = [];
    ws.onopen = () => {
      if (socket.current === ws)
        ws.send(
          JSON.stringify({
            type: "history",
            thread_id: item.thread_id,
            before: history.before,
          }),
        );
    };
    ws.onmessage = (event) => {
      if (socket.current !== ws) return;
      try {
        const frame = JSON.parse(event.data);
        if (
          frame.type === "error" &&
          frame.code === "canonical_chat_frame_unavailable" &&
          !page.length
        ) {
          socket.current = undefined;
          ws.close();
          setHistoryUnsupported(true);
          setOlder(null);
          setPhase("ready");
          return;
        }
        if (frame.thread_id !== item.thread_id) throw new Error();
        if (
          frame.type === "history_turn" &&
          typeof frame.execution_id === "string" &&
          typeof frame.question === "string" &&
          typeof frame.state === "string" &&
          Number.isSafeInteger(frame.event_seq)
        ) {
          page.push({
            id: frame.execution_id,
            seq: frame.event_seq,
            question: frame.question,
            answer: "",
            state: frame.state,
          });
        } else if (
          frame.type === "history_text" &&
          typeof frame.content === "string" &&
          page.length &&
          page[page.length - 1].id === frame.execution_id
        ) {
          page[page.length - 1].answer += frame.content;
        } else if (
          frame.type === "history_end" &&
          (frame.before === null || typeof frame.before === "string")
        ) {
          socket.current = undefined;
          ws.close();
          setTurns((previous) =>
            history.before ? [...page, ...previous] : page,
          );
          setOlder(frame.before);
          const last = page[page.length - 1];
          if (
            !history.before &&
            last &&
            [
              "queued",
              "running",
              "cancel_requested",
              "awaiting_approval",
            ].includes(last.state)
          ) {
            observed.current = {
              run: last.id,
              seq: last.seq,
              index: Number.MAX_SAFE_INTEGER,
            };
            connect();
          } else {
            observed.current = undefined;
            setPhase(
              !history.before && last?.state === "unknown"
                ? "unknown"
                : "ready",
            );
          }
        } else throw new Error();
      } catch {
        ws.close();
      }
    };
    ws.onclose = () => {
      if (socket.current !== ws) return;
      socket.current = undefined;
      setTurns([]);
      setOlder(null);
      setPhase("historyError");
    };
    return () => {
      if (socket.current === ws) socket.current = undefined;
      ws.close();
    };
  }, [item.agent_id, item.thread_id, history, connect]);
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Typography.Title level={4}>{item.title}</Typography.Title>
      {older && phase === "ready" && (
        <Button onClick={() => loadHistory({ before: older })}>
          {t("canonical.olderTurns")}
        </Button>
      )}
      {phase === "historyError" && (
        <Alert type="warning" message={t("canonical.historyUnavailable")} />
      )}
      {historyUnsupported && (
        <Typography.Text type="secondary">
          {t("canonical.historyUnsupported")}
        </Typography.Text>
      )}
      {(phase === "historyError" ||
        (phase === "unknown" && !observed.current)) && (
        <Button onClick={() => loadHistory({ before: null })}>
          {t("canonical.reloadTurns")}
        </Button>
      )}
      <div role="log">
        {turns.map((turn, i) => (
          <div key={i}>
            <p>{turn.question}</p>
            <p>{turn.answer}</p>
            {turn.state &&
              ["failed", "cancelled", "unknown"].includes(turn.state) && (
                <Typography.Text type="secondary">
                  {t(`canonical.turnState_${turn.state}`)}
                </Typography.Text>
              )}
          </div>
        ))}
      </div>
      {phase === "unknown" && (
        <Alert type="warning" message={t("canonical.turnUnconfirmed")} />
      )}
      {phase === "unknown" && observed.current && (
        <Button onClick={() => connect()}>
          {t("canonical.resumeObservation")}
        </Button>
      )}
      <Input.TextArea
        aria-label={t("canonical.message")}
        value={draft}
        disabled={phase !== "ready"}
        onChange={(event) => setDraft(event.target.value)}
        autoSize={{ minRows: 2, maxRows: 7 }}
      />
      <Space>
        <Button
          type="primary"
          disabled={phase !== "ready" || !draft.trim()}
          onClick={() => connect(draft.trim())}
        >
          {t("canonical.sendMessage")}
        </Button>
        <Button onClick={back}>{t("canonical.backToConversations")}</Button>
      </Space>
    </Space>
  );
}
