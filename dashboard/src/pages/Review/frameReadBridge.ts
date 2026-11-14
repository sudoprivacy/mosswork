import type { ServedReviewDocument } from "../../api/modules/humanReview";
import {
  buildHostReadBridgeScript,
  type HostReadBinding,
} from "./shareone/host-read-script";
import { buildSandboxedHtmlDocument } from "./shareone/sandbox-document";

export interface FrameReadReply {
  status: number;
  body: unknown;
}

/** Presentation bytes are separate from the immutable original HTML/record. */
export function buildReviewFrameDocument(
  document: ServedReviewDocument,
  binding: HostReadBinding,
  origin: string,
): string {
  return buildSandboxedHtmlDocument({
    html: document.html,
    headScripts: [buildHostReadBridgeScript(binding, origin)],
  });
}

/** Existing ShareOne wire; only the active authenticated parent selects HTTP. */
export class ReviewFrameReads {
  private disposed = false;
  private readonly pending = new Map<
    string,
    {
      controller: AbortController;
      timeout: ReturnType<typeof setTimeout>;
    }
  >();
  private readonly listener = (event: MessageEvent) => void this.handle(event);
  private readonly binding: HostReadBinding;
  private readonly frame: () => HTMLIFrameElement | null;
  private readonly current: () => boolean;
  private readonly read: (
    request: Record<string, unknown>,
    signal: AbortSignal,
  ) => Promise<FrameReadReply>;
  private readonly withdraw: () => void;

  constructor(
    binding: HostReadBinding,
    frame: () => HTMLIFrameElement | null,
    current: () => boolean,
    read: (
      request: Record<string, unknown>,
      signal: AbortSignal,
    ) => Promise<FrameReadReply>,
    withdraw: () => void,
  ) {
    this.binding = binding;
    this.frame = frame;
    this.current = current;
    this.read = read;
    this.withdraw = withdraw;
    window.addEventListener("message", this.listener);
  }

  dispose(): void {
    this.disposed = true;
    window.removeEventListener("message", this.listener);
    for (const { controller, timeout } of this.pending.values()) {
      clearTimeout(timeout);
      controller.abort();
    }
    this.pending.clear();
  }

  private live(target: Window): boolean {
    return (
      !this.disposed && this.current() && this.frame()?.contentWindow === target
    );
  }

  private send(target: Window, id: string, result: FrameReadReply): void {
    if (!this.live(target)) return;
    target.postMessage(
      {
        source: "shareone-host-read-result",
        shareId: this.binding.shareId,
        channel: this.binding.channel,
        id,
        result,
      },
      "*", // The exact checked frame has an opaque sandbox origin.
    );
  }

  private async handle(event: MessageEvent): Promise<void> {
    const target = this.frame()?.contentWindow;
    const message = event.data;
    if (
      !target ||
      !this.live(target) ||
      event.source !== target ||
      event.origin !== "null" ||
      !message ||
      message.source !== "shareone-host-read" ||
      message.shareId !== this.binding.shareId ||
      message.channel !== this.binding.channel ||
      typeof message.id !== "string" ||
      !/^[0-9]{1,12}$/.test(message.id) ||
      !message.body ||
      typeof message.body !== "object" ||
      Array.isArray(message.body) ||
      this.pending.has(message.id)
    )
      return;
    let serialized: string;
    try {
      serialized = JSON.stringify(message.body);
    } catch {
      return;
    }
    if (
      new TextEncoder().encode(serialized).byteLength > 65536 ||
      this.pending.size >= 8
    ) {
      this.send(target, message.id, {
        status: 429,
        body: { error: "LIVE_READ_LIMIT" },
      });
      return;
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => {
      controller.abort();
      if (this.live(target)) {
        this.dispose();
        this.withdraw();
      }
    }, 30000);
    this.pending.set(message.id, { controller, timeout });
    try {
      const result = await this.read(JSON.parse(serialized), controller.signal);
      if (!this.live(target)) return;
      if (controller.signal.aborted || result.status !== 200) throw new Error();
      this.send(target, message.id, result);
    } catch {
      if (this.live(target)) {
        this.dispose();
        this.withdraw();
      }
    } finally {
      clearTimeout(timeout);
      this.pending.delete(message.id);
    }
  }
}
