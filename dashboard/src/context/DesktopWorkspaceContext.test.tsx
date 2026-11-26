import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  DesktopWorkspaceProvider,
  useDesktopWorkspace,
} from "./DesktopWorkspaceContext";
import { devicesApi } from "../api/modules/devices";
import { withReadyDesktopDevice } from "../api/desktopDeviceReady";
import type { DesktopWorkspaceState } from "../api/desktopWorkspace";
import { StrictMode } from "react";

vi.mock("../api/modules/devices", () => ({
  devicesApi: { list: vi.fn(), pair: vi.fn(), renewal: vi.fn() },
}));
vi.mock("../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: 7 }),
}));
const initial: DesktopWorkspaceState = {
  state: "unpaired",
  device_id: "",
  owner_user_id: 0,
  path: "",
  grant: null,
};
const connected: DesktopWorkspaceState = {
  ...initial,
  state: "online",
  owner_user_id: 7,
  device_id: "this-computer",
  path: "/work",
  grant: {
    id: "work",
    name: "Work",
    version: 1,
    writable: true,
    allow_company: true,
  },
};
function Probe() {
  const desktop = useDesktopWorkspace();
  return (
    <>
      <div>Chat available</div>
      <output>{desktop.state?.path || desktop.error || "waiting"}</output>
      <button onClick={() => void desktop.choose()}>Choose folder</button>
      <button onClick={desktop.retry}>Retry</button>
    </>
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(devicesApi.list).mockResolvedValue({ enabled: true, devices: [] });
  vi.mocked(devicesApi.pair).mockResolvedValue({
    code: "mosswork-pair.once",
    expires_at: 9999999999,
    credential_expires_at: 9999999999,
  });
  window.mossworkDesktopWorkspace = {
    status: vi.fn().mockResolvedValue(initial),
    pair: vi.fn().mockResolvedValue(connected),
    choose: vi.fn().mockResolvedValue({ ...connected, path: "/new-work" }),
    clear: vi.fn(),
    disconnect: vi.fn().mockResolvedValue(initial),
  };
});
describe("employee desktop connection", () => {
  it("joins background recovery on wake and releases only uncancelled sends", async () => {
    const native = window.mossworkDesktopWorkspace!;
    vi.mocked(native.status).mockResolvedValue({
      ...connected,
      state: "disconnected",
      renew_at: 1,
      expires_at: 1,
    });
    vi.mocked(devicesApi.renewal).mockResolvedValue({ ticket: "once" });
    let complete!: (state: DesktopWorkspaceState) => void;
    native.renew = vi.fn(
      () =>
        new Promise<DesktopWorkspaceState>((resolve) => {
          complete = resolve;
        }),
    );
    render(
      <DesktopWorkspaceProvider>
        <Probe />
      </DesktopWorkspaceProvider>,
    );
    await waitFor(() => expect(native.renew).toHaveBeenCalledOnce());
    const first = vi.fn(),
      second = vi.fn();
    const controller = new AbortController();
    const cancelled = withReadyDesktopDevice(first, {
      signal: controller.signal,
    });
    const pending = withReadyDesktopDevice(second);
    controller.abort();
    await expect(cancelled).rejects.toMatchObject({ name: "AbortError" });
    expect(second).not.toHaveBeenCalled();
    await act(async () => complete(connected));
    await pending;
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledOnce();
    expect(devicesApi.renewal).toHaveBeenCalledOnce();
  });

  it("recovers cold pairing after a failed network request without a settings visit", async () => {
    vi.mocked(devicesApi.list).mockRejectedValueOnce(new Error("offline"));
    render(
      <DesktopWorkspaceProvider>
        <Probe />
      </DesktopWorkspaceProvider>,
    );
    await screen.findByText("connection_failed");
    const send = vi.fn();
    await act(async () => {
      await withReadyDesktopDevice(send);
    });
    expect(devicesApi.pair).toHaveBeenCalledOnce();
    expect(send).toHaveBeenCalledOnce();
  });
  it.each(["online", "disconnected"] as const)(
    "renews a due %s device without pairing",
    async (state) => {
      const native = window.mossworkDesktopWorkspace!;
      native.renew = vi.fn().mockResolvedValue(connected);
      vi.mocked(native.status).mockResolvedValue({
        ...connected,
        state,
        renew_at: 1,
      });
      vi.mocked(devicesApi.renewal).mockResolvedValue({
        ticket: "mosswork-renew.test",
      });
      render(
        <DesktopWorkspaceProvider>
          <Probe />
        </DesktopWorkspaceProvider>,
      );
      await waitFor(() =>
        expect(native.renew).toHaveBeenCalledWith("mosswork-renew.test"),
      );
      expect(devicesApi.renewal).toHaveBeenCalledWith(connected.device_id);
      expect(native.pair).not.toHaveBeenCalled();
      expect(native.disconnect).not.toHaveBeenCalled();
    },
  );

  it("does not silently re-pair a revoked device", async () => {
    const native = window.mossworkDesktopWorkspace!;
    vi.mocked(native.status).mockResolvedValue({
      ...initial,
      state: "disconnected",
      pair_blocked: true,
    });
    render(
      <DesktopWorkspaceProvider>
        <Probe />
      </DesktopWorkspaceProvider>,
    );
    await waitFor(() => expect(devicesApi.list).toHaveBeenCalled());
    expect(native.pair).not.toHaveBeenCalled();
  });
  it("pairs on sign-in and updates the folder inside settings", async () => {
    render(
      <DesktopWorkspaceProvider>
        <Probe />
      </DesktopWorkspaceProvider>,
    );
    expect(await screen.findByText("/work")).toBeInTheDocument();
    expect(window.mossworkDesktopWorkspace!.pair).toHaveBeenCalledWith(
      "mosswork-pair.once",
    );
    await act(async () => screen.getByText("Choose folder").click());
    expect(screen.getByText("/new-work")).toBeInTheDocument();
  });
  it("connects when the native bridge arrives after React", async () => {
    const bridge = window.mossworkDesktopWorkspace;
    delete window.mossworkDesktopWorkspace;
    render(
      <DesktopWorkspaceProvider>
        <Probe />
      </DesktopWorkspaceProvider>,
    );
    expect(devicesApi.pair).not.toHaveBeenCalled();
    await act(async () => {
      window.mossworkDesktopWorkspace = bridge;
      window.dispatchEvent(new Event("mosswork:desktop-workspace-ready"));
    });
    expect(await screen.findByText("/work")).toBeInTheDocument();
  });
  it("disconnects a mismatched account before binding this login", async () => {
    vi.mocked(window.mossworkDesktopWorkspace!.status).mockResolvedValue({
      ...connected,
      owner_user_id: 8,
    });
    render(
      <DesktopWorkspaceProvider>
        <Probe />
      </DesktopWorkspaceProvider>,
    );
    await screen.findByText("/work");
    expect(window.mossworkDesktopWorkspace!.disconnect).toHaveBeenCalledOnce();
  });
  it("leaves server chat usable when local connection fails", async () => {
    vi.mocked(window.mossworkDesktopWorkspace!.pair).mockRejectedValue(
      new Error("network"),
    );
    render(
      <DesktopWorkspaceProvider>
        <Probe />
      </DesktopWorkspaceProvider>,
    );
    await screen.findByText("connection_failed");
    expect(screen.getByText("Chat available")).toBeInTheDocument();
  });
  it("does not require pairing when the administrator disabled local tools", async () => {
    vi.mocked(devicesApi.list).mockResolvedValue({
      enabled: false,
      devices: [],
    });
    render(
      <DesktopWorkspaceProvider>
        <Probe />
      </DesktopWorkspaceProvider>,
    );
    await waitFor(() => expect(devicesApi.list).toHaveBeenCalledOnce());
    expect(devicesApi.pair).not.toHaveBeenCalled();
  });
  it("does not consume two pairing codes under StrictMode", async () => {
    render(
      <StrictMode>
        <DesktopWorkspaceProvider>
          <Probe />
        </DesktopWorkspaceProvider>
      </StrictMode>,
    );
    await waitFor(() => expect(screen.getByText("/work")).toBeInTheDocument(), {
      timeout: 4500,
    });
    expect(devicesApi.pair).toHaveBeenCalledOnce();
  });
});
