import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBrowserStream } from "./useBrowserStream";

const auth = vi.hoisted(() => ({ token: "local-test-token" }));
vi.mock("../api/request", () => ({
  getAuthToken: () => auth.token,
  AUTH_TOKEN_CHANGED_EVENT: "mosswork:auth-token-changed",
  UNAUTHORIZED_EVENT: "mosswork:unauthorized",
}));

class StreamSocket {
  static OPEN = 1;
  static CLOSING = 2;
  static instances: StreamSocket[] = [];
  readyState = 1;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  constructor() {
    StreamSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  push(value: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(value) });
  }
}

function setup() {
  const decoded: {
    data: string;
    current: () => boolean;
    finish: () => void;
  }[] = [];
  const painted: string[] = [];
  const onFrame = vi.fn(
    (data: string, current: () => boolean) =>
      new Promise<boolean>((resolve) => {
        decoded.push({
          data,
          current,
          finish: () => {
            if (current()) painted.push(data);
            resolve(current());
          },
        });
      }),
  );
  const onError = vi.fn();
  const hook = renderHook(() => useBrowserStream());
  const connect = () => {
    act(() => hook.result.current.connect("", 1280, 800, { onFrame, onError }));
    const socket = StreamSocket.instances.at(-1)!;
    act(() => socket.onopen?.());
    return socket;
  };
  const update = (
    socket: StreamSocket,
    controlEpoch: number,
    targetEpoch = 0,
    owner = "user",
  ) =>
    act(() => {
      socket.push({ type: "status", status: "streaming" });
      socket.push({
        type: "session_update",
        session_id: "user-42",
        control_owner: owner,
        controlEpoch,
        targetEpoch,
      });
    });
  const frame = (
    socket: StreamSocket,
    data: string,
    controlEpoch: number,
    targetEpoch = 0,
  ) =>
    act(() => {
      socket.push({ type: "frame", data, controlEpoch, targetEpoch });
    });
  const finish = async (index: number) => {
    await act(async () => {
      decoded[index].finish();
    });
  };
  return {
    ...hook,
    decoded,
    painted,
    onFrame,
    onError,
    connect,
    update,
    frame,
    finish,
  };
}

beforeEach(() => {
  auth.token = "local-test-token";
  StreamSocket.instances = [];
  vi.stubGlobal("WebSocket", StreamSocket);
});
afterEach(() => vi.unstubAllGlobals());

describe("browser stream displayed-frame boundary", () => {
  it("does not reopen a viewer after logout while its component stays mounted", () => {
    const view = setup();
    auth.token = "";
    act(() =>
      view.result.current.connect("", 1280, 800, { onFrame: view.onFrame }),
    );
    expect(StreamSocket.instances).toHaveLength(0);
    expect(view.result.current.status).toBe("idle");
  });

  it.each(["mosswork:auth-token-changed", "storage", "focus"])(
    "disconnects on logout observed through %s and invalidates pending paint and input",
    async (event) => {
      const view = setup();
      const socket = view.connect();
      view.update(socket, 1, 3);
      view.frame(socket, "before-logout", 1, 3);
      await view.finish(0);
      expect(view.result.current.inputReady).toBe(true);
      view.frame(socket, "pending-at-logout", 1, 3);
      auth.token = "";
      act(() => window.dispatchEvent(new Event(event)));
      expect(socket.readyState).toBe(3);
      expect(view.result.current.sessionInfo).toBeNull();
      expect(view.result.current.inputReady).toBe(false);
      expect(
        view.result.current.sendEvent({ type: "keyup", key: "Shift" }),
      ).toBe(false);
      await view.finish(1);
      expect(view.painted).toEqual(["before-logout"]);
      view.update(socket, 99, 99);
      expect(view.result.current.status).toBe("idle");
    },
  );

  it("withdraws an unauthorized viewer without treating sliding token renewal as logout", async () => {
    const view = setup();
    const socket = view.connect();
    view.update(socket, 1);
    view.frame(socket, "current", 1);
    await view.finish(0);
    auth.token = "renewed-local-test-token";
    act(() => window.dispatchEvent(new Event("mosswork:auth-token-changed")));
    expect(socket.readyState).toBe(StreamSocket.OPEN);
    expect(view.result.current.inputReady).toBe(true);
    act(() => window.dispatchEvent(new Event("mosswork:unauthorized")));
    expect(socket.readyState).toBe(3);
    expect(view.result.current.sessionInfo).toBeNull();
    expect(
      view.result.current.sendEvent({ type: "type", text: "refused" }),
    ).toBe(false);
  });

  it("can release a held key after a refused target change without replaying input", async () => {
    const view = setup();
    const socket = view.connect();
    view.update(socket, 1, 3);
    view.frame(socket, "held-key-page", 1, 3);
    await view.finish(0);
    expect(
      view.result.current.sendEvent({
        type: "keydown",
        key: "Shift",
        code: "ShiftLeft",
      }),
    ).toBe(true);
    act(() => {
      view.result.current.sendEvent({
        type: "resize",
        width: 390,
        height: 700,
      });
    });
    const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
    act(() => {
      socket.push({
        type: "error",
        message: "browser_input_in_progress",
        recoverable: true,
      });
    });
    errorLog.mockRestore();
    expect(
      view.result.current.sendEvent({
        type: "keyup",
        key: "Shift",
        code: "ShiftLeft",
      }),
    ).toBe(true);
    expect(socket.sent.at(-1)).toMatchObject({
      controlEpoch: 1,
      targetEpoch: 3,
      type: "keyup",
    });
    expect(
      view.result.current.sendEvent({ type: "type", text: "do not replay" }),
    ).toBe(false);
    view.frame(socket, "same-target-after-release", 1, 3);
    await view.finish(1);
    expect(view.result.current.inputReady).toBe(true);
    expect(socket.sent.filter((event) => event.type === "resize")).toHaveLength(
      1,
    );
  });

  it("fences input immediately during resize and waits for the changed target to paint", async () => {
    const view = setup();
    const socket = view.connect();
    view.update(socket, 1, 3);
    view.frame(socket, "before-resize", 1, 3);
    await view.finish(0);
    act(() => {
      expect(
        view.result.current.sendEvent({
          type: "resize",
          width: 390,
          height: 700,
        }),
      ).toBe(true);
    });
    expect(view.result.current.inputReady).toBe(false);
    expect(
      view.result.current.sendEvent({ type: "mousedown", x: 12, y: 12 }),
    ).toBe(false);
    expect(
      view.result.current.sendEvent({ type: "type", text: "do not replay" }),
    ).toBe(false);
    // A release can drain a pre-existing held key if the resize was refused.
    expect(
      view.result.current.sendEvent({
        type: "keyup",
        key: "Shift",
        code: "ShiftLeft",
      }),
    ).toBe(true);
    view.frame(socket, "already-in-flight-old-size", 1, 3);
    await view.finish(1);
    expect(view.result.current.inputReady).toBe(false);
    view.update(socket, 1, 4);
    view.frame(socket, "new-size", 1, 4);
    await view.finish(2);
    expect(view.result.current.inputReady).toBe(true);
    expect(view.result.current.sendEvent({ type: "click", x: 13, y: 14 })).toBe(
      true,
    );
    expect(socket.sent.at(-1)).toMatchObject({ targetEpoch: 4 });
    expect(socket.sent.some((event) => event.text === "do not replay")).toBe(
      false,
    );
  });

  it("waits for actual paint and fences old pixels across a control/target change", async () => {
    const view = setup();
    const socket = view.connect();
    view.update(socket, 1);
    view.frame(socket, "old-page", 1);
    expect(view.result.current.inputReady).toBe(false);
    expect(view.result.current.sendEvent({ type: "click", x: 10, y: 20 })).toBe(
      false,
    );
    await view.finish(0);
    expect(view.result.current.inputReady).toBe(true);
    expect(view.result.current.sendEvent({ type: "click", x: 10, y: 20 })).toBe(
      true,
    );
    expect(socket.sent.at(-1)).toMatchObject({
      controlEpoch: 1,
      targetEpoch: 0,
    });
    view.update(socket, 2, 1);
    expect(view.result.current.inputReady).toBe(false);
    expect(view.result.current.sendEvent({ type: "click", x: 10, y: 20 })).toBe(
      false,
    );
    view.frame(socket, "stale-page", 1);
    expect(view.onFrame).toHaveBeenCalledTimes(1);
    view.frame(socket, "new-page", 2, 1);
    expect(view.result.current.sendEvent({ type: "type", text: "draft" })).toBe(
      false,
    );
    await view.finish(1);
    expect(view.result.current.inputReady).toBe(true);
    expect(view.result.current.sendEvent({ type: "type", text: "draft" })).toBe(
      true,
    );
    expect(socket.sent.at(-1)).toMatchObject({
      controlEpoch: 2,
      targetEpoch: 1,
      text: "draft",
    });
    expect(view.painted).toEqual(["old-page", "new-page"]);
  });

  it("ignores late image decodes and events from a replaced connection", async () => {
    const view = setup();
    const old = view.connect();
    view.update(old, 1);
    view.frame(old, "late-old-image", 1);
    const current = view.connect();
    view.update(current, 3);
    view.frame(current, "current-image", 3);
    await view.finish(1);
    await view.finish(0);
    act(() => {
      old.push({
        type: "session_update",
        control_owner: "agent",
        controlEpoch: 99,
        targetEpoch: 99,
      });
      old.onerror?.();
      old.onclose?.();
    });
    expect(view.painted).toEqual(["current-image"]);
    expect(view.result.current.status).toBe("streaming");
    expect(view.result.current.sessionInfo?.controlEpoch).toBe(3);
    expect(view.result.current.sendEvent({ type: "click", x: 1, y: 2 })).toBe(
      true,
    );
    expect(current.sent.at(-1)).toMatchObject({ controlEpoch: 3 });
  });

  it("keeps recoverable refusals connected and rejects input during AI ownership", async () => {
    const view = setup();
    const socket = view.connect();
    view.update(socket, 4, 0, "agent");
    view.frame(socket, "ai-view", 4);
    await view.finish(0);
    expect(
      view.result.current.sendEvent({
        type: "resize",
        width: 800,
        height: 600,
      }),
    ).toBe(false);
    expect(view.result.current.sendEvent({ type: "click", x: 1, y: 2 })).toBe(
      false,
    );
    const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
    act(() =>
      socket.push({
        type: "error",
        message: "stale_browser_operation",
        recoverable: true,
      }),
    );
    errorLog.mockRestore();
    expect(view.result.current.status).toBe("streaming");
    expect(view.onError).toHaveBeenCalledWith("stale_browser_operation");
    view.update(socket, 5);
    view.frame(socket, "human-view", 5);
    await view.finish(1);
    expect(
      view.result.current.sendEvent({
        type: "resize",
        width: 800,
        height: 600,
      }),
    ).toBe(true);
    view.unmount();
    expect(socket.readyState).toBe(3);
  });
});
