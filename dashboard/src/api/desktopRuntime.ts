import type { HarnessChunk } from "../utils/parseHarnessChunk";
import type { HitlPendingPayload } from "./types/hitl";
import { withReadyDesktopDevice } from "./desktopDeviceReady";

export interface LocalArtifact {
  path: string;
  size: number;
  sha256: string;
  explicitly_sent?: boolean;
}
export interface LocalArtifactTurn {
  task_id: string;
  user_message_id: string;
  message_id?: string | null;
  device_id?: string;
  files: LocalArtifact[];
}
export interface RuntimeEvent {
  event:
    | "chunk"
    | "done"
    | "runtime_exit"
    | "steer"
    | "process"
    | "process_output"
    | "model_status"
    | "runtime_progress";
  thread_id?: string;
  task_id?: string;
  data?: HarnessChunk;
  frame?: Record<string, unknown>;
  status?:
    | "completed"
    | "failed"
    | "cancelled"
    | "unknown"
    | "waiting"
    | "retrying";
  attempt?: number;
  max_attempts?: number;
  error?: string;
  error_code?: string;
  stage?: string;
  retryable?: boolean;
  action?: string;
  artifacts?: LocalArtifact[];
  sync_state?: "pending" | "synced";
  pid?: number;
  stream?: "stdout" | "stderr";
  content?: string;
  tool_call_id?: string;
  phase?: string;
}
export interface RuntimeStart {
  task_id: string;
  thread_id: string;
  agent_id: string;
  text: string;
  local_attachments?: LocalArtifact[];
  edit_message_id?: string;
  model?: string;
  knowledge_base_ids?: string[];
  reasoning_mode?: string;
  reasoning_effort?: string;
  conversation_mode?: string;
  hitl_policy?: { mode: string; tools?: string[] } | null;
  mcp_servers?: string[];
}
export interface DesktopRuntimeBridge {
  version: 1;
  /** Read active work without starting tools or requiring project pairing. */
  refreshStatus?(): Promise<{
    active_tasks: { task_id: string; status?: string }[];
  }>;
  status(options?: { verify_tools: boolean }): Promise<{
    active_tasks: {
      task_id: string;
      thread_id: string;
      status?: string;
      recovery_state?: string;
      sync_state?: "pending" | "synced";
      pending_approval?: HitlPendingPayload | null;
    }[];
    capabilities?: string[];
    tools?: Record<string, string>;
    execute_authorized?: boolean;
  }>;
  start(params: RuntimeStart): Promise<{ task_id: string }>;
  createThread?(params: { agent_id: string }): Promise<{
    thread_id: string;
    session_key: string;
    execution_mode: "local";
    continuation_of_thread_id?: string | null;
  }>;
  cancel(params: { thread_id: string }): Promise<{ status: string }>;
  resumeHitl?(params: {
    thread_id: string;
    pending_id: string;
    decisions: Array<{ type: string; message?: string }>;
    hitl_policy?: { mode: string; tools?: string[] };
  }): Promise<{ status: string }>;
  steer?(params: {
    thread_id: string;
    text: string;
    expected_turn_id: string;
    client_pending_id: string;
  }): Promise<Record<string, unknown>>;
  removeSteer?(params: {
    thread_id: string;
    turn_id: string;
    client_pending_id: string;
    pending_id: string;
  }): Promise<Record<string, unknown>>;
  threadWorkspace?(params: {
    thread_id: string;
    agent_id: string;
    token: string;
    path: string;
    reveal?: boolean;
  }): Promise<{
    root: string;
    entries: import("./modules/mossworkThreads").ThreadDirectoryEntry[];
  } | null>;
  reveal(params: { path: string; sha256?: string }): Promise<void>;
  previewImage?(params: {
    path: string;
    sha256: string;
  }): Promise<{ data_url: string; width: number; height: number }>;
  uploadArtifact?(params: LocalArtifact & { token: string }): Promise<{
    upload_id: string;
    asset_id: string;
  }>;
  importAttachment?(
    params: {
      thread_id: string;
      filename?: string;
      token: string;
    } & (
      | { asset_id: string }
      | { path: string; sha256: string }
      | { agent_id: string; workspace_path: string }
    ),
  ): Promise<LocalArtifact>;
  reconcile?(params: {
    thread_id: string;
    task_id?: string;
    resolution: "failed" | "cancelled";
  }): Promise<{ status: string }>;
  subscribe(listener: (event: RuntimeEvent) => void): () => void;
}
declare global {
  interface Window {
    __mossworkRuntime?: DesktopRuntimeBridge;
  }
}
// Company intranet HTTP origins may expose getRandomValues but not randomUUID.
export function createRuntimeTaskId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(
    12,
    16,
  )}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function isCompanyDesktop(): boolean {
  return (
    import.meta.env.VITE_DESKTOP_BUNDLE === "1" ||
    Boolean(window.mossworkDesktopWorkspace || window.__mossworkRuntime)
  );
}
export function requireDesktopRuntime(): DesktopRuntimeBridge {
  const runtime = window.__mossworkRuntime;
  if (!runtime || runtime.version !== 1) {
    throw new Error(
      "当前企业客户端缺少本地执行组件，请安装完整企业客户端；通讯录和员工聊天仍可使用。",
    );
  }
  return runtime;
}

/** Preserve task ownership when a thread is reused for another native turn. */
export function matchesRuntimeTask(
  event: RuntimeEvent,
  threadId: string,
  taskId?: string,
): boolean {
  if (event.event === "runtime_progress") return true;
  return (
    event.thread_id === threadId &&
    (!taskId || !event.task_id || event.task_id === taskId)
  );
}

const LOCAL_RUNTIME_PREPARATION_RETRY_DELAYS_MS = [500, 1_500] as const;

type RuntimeStartError = Error & {
  errorCode?: string;
  stage?: string;
  retryable?: boolean;
};

function shouldRetryRuntimePreparation(error: unknown): boolean {
  const runtimeError = error as RuntimeStartError;
  return (
    error instanceof Error &&
    runtimeError.stage === "runtime_preparing" &&
    runtimeError.retryable === true
  );
}

function waitForRuntimePreparationRetry(
  signal: AbortSignal,
  delayMs: number,
): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(done, delayMs);
    function done() {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done, { once: true });
  });
}

export async function readLocalTaskReceipt(
  runtime: DesktopRuntimeBridge,
  threadId: string,
  taskId: string,
  signal: AbortSignal,
) {
  const snapshot = await withReadyDesktopDevice(() => runtime.status(), {
    signal,
  });
  return snapshot.active_tasks.find(
    (task) => task.task_id === taskId && task.thread_id === threadId,
  );
}

/** Subscribe before starting: a short task may complete before its start reply. */
export async function runLocalTurn(
  params: RuntimeStart,
  onEvent: (event: RuntimeEvent) => void,
  signal: AbortSignal,
  onStarted?: () => void,
): Promise<void> {
  const runtime = requireDesktopRuntime();
  return new Promise<void>((resolve, reject) => {
    let finished = false;
    let acknowledged = false;
    let interruption: Error | undefined;
    let checking = false;
    const receiptWait = new AbortController();
    const finish = (error?: Error) => {
      if (finished) return;
      finished = true;
      receiptWait.abort();
      unsubscribe();
      signal.removeEventListener("abort", abort);
      if (error) reject(error);
      else resolve();
    };
    const checkReceipt = async (failure: Error) => {
      if (finished || checking) return;
      checking = true;
      try {
        const receipt = await readLocalTaskReceipt(
          runtime,
          params.thread_id,
          params.task_id,
          receiptWait.signal,
        );
        if (finished) return;
        if (
          receipt?.status === "running" ||
          receipt?.status === "cancel_requested"
        ) {
          acknowledged = true;
          onStarted?.();
        } else if (
          receipt?.status === "completed" ||
          receipt?.status === "cancelled"
        ) {
          onEvent({
            event: "done",
            task_id: params.task_id,
            thread_id: params.thread_id,
            status: receipt.status,
            sync_state: receipt.sync_state,
          });
          finish();
        } else finish(failure);
      } catch {
        finish(failure);
      } finally {
        checking = false;
      }
    };
    const unsubscribe = runtime.subscribe((event) => {
      if (event.event === "runtime_exit") {
        const failure = new Error(
          event.error || "本地运行时已退出，请核对任务状态和生成文件。",
        );
        if (event.error_code === "device_lease_interrupted") {
          interruption = failure;
          if (acknowledged) void checkReceipt(failure);
        } else finish(failure);
        return;
      }
      if (!matchesRuntimeTask(event, params.thread_id, params.task_id)) return;
      onEvent(event);
      if (event.event === "done") {
        if (["failed", "unknown"].includes(event.status ?? "")) {
          const error = new Error(
            event.error || "本地任务未完成，请检查公司连接和本地工具状态。",
          ) as Error & { errorCode?: string };
          error.errorCode = event.error_code;
          finish(error);
        } else {
          finish();
        }
      }
    });
    const abort = () => {
      receiptWait.abort();
      void runtime
        .cancel({ thread_id: params.thread_id })
        .catch((err: unknown) => {
          finish(
            err instanceof Error
              ? err
              : new Error("取消未确认，请核对任务状态。"),
          );
        });
    };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) {
      finish(new Error("任务已取消"));
      return;
    }
    void (async () => {
      let preparationRetry = 0;
      while (!finished && !signal.aborted) {
        try {
          await runtime.start(params);
          acknowledged = true;
          if (interruption) await checkReceipt(interruption);
          else if (!finished) onStarted?.();
          return;
        } catch (err) {
          if (
            preparationRetry <
              LOCAL_RUNTIME_PREPARATION_RETRY_DELAYS_MS.length &&
            shouldRetryRuntimePreparation(err) &&
            !signal.aborted
          ) {
            await waitForRuntimePreparationRetry(
              signal,
              LOCAL_RUNTIME_PREPARATION_RETRY_DELAYS_MS[preparationRetry],
            );
            preparationRetry += 1;
            continue;
          }
          if (signal.aborted) {
            finish();
            return;
          }
          if (
            !finished &&
            (err as RuntimeStartError)?.errorCode === "task_result_unknown"
          ) {
            await checkReceipt(interruption ?? (err as Error));
            return;
          }
          finish(err instanceof Error ? err : new Error("无法启动本地任务。"));
          return;
        }
      }
      if (signal.aborted) finish();
    })();
  });
}
