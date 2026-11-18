import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { mossworkThreadsApi } from "../../../api/modules/mossworkThreads";
import * as request from "../../../api/request";
import ThreadWorkspacePanel from "./ThreadWorkspacePanel";

const props = {
  agentId: "expert",
  threadId: "thr_a",
  streaming: false,
  onOpenFile: vi.fn(),
};
const root = {
  location: "server" as const,
  root: "threads/thr_a",
  display_root: "/srv/expert/threads/thr_a",
  entries: [
    { name: "generated", is_dir: true },
    { name: "inputs", is_dir: true },
  ],
  legacy_files: [{ path: "/old/note.txt", agent_id: "expert" }],
  has_messages: true,
};

afterEach(() => {
  delete window.__mossworkRuntime;
  vi.restoreAllMocks();
  props.onOpenFile.mockClear();
});

it("browses a server thread on the desktop and opens its original legacy file", async () => {
  const native = vi.fn();
  Object.defineProperty(window, "__mossworkRuntime", {
    configurable: true,
    value: { threadWorkspace: native },
  });
  const list = vi
    .spyOn(mossworkThreadsApi, "workspace")
    .mockResolvedValueOnce(root)
    .mockResolvedValueOnce({
      ...root,
      entries: [{ name: "new.txt", is_dir: false }],
      legacy_files: [],
    });
  render(<ThreadWorkspacePanel {...props} />);
  fireEvent.click(await screen.findByText("note.txt"));
  expect(props.onOpenFile).toHaveBeenCalledWith("/old/note.txt", "expert");
  expect(screen.queryByText(root.display_root)).not.toBeInTheDocument();
  expect(screen.queryByTitle("/old/note.txt")).not.toBeInTheDocument();
  expect(list.mock.calls).toEqual([["expert", "thr_a", ""]]);
  fireEvent.click(screen.getByText("generated"));
  fireEvent.click(await screen.findByText("new.txt"));
  expect(screen.getByText("inputs")).toBeVisible();
  expect(
    screen.getByText("generated").closest('[role="treeitem"]'),
  ).toHaveAttribute("aria-expanded", "true");
  expect(list).toHaveBeenLastCalledWith("expert", "thr_a", "generated");
  expect(props.onOpenFile).toHaveBeenLastCalledWith(
    "threads/thr_a/generated/new.txt",
    "expert",
  );
  expect(native).not.toHaveBeenCalled();
});

it("opens a local file through the native workspace for this thread", async () => {
  vi.spyOn(request, "getAuthToken").mockReturnValue("session-token");
  vi.spyOn(mossworkThreadsApi, "workspace").mockResolvedValue({
    ...root,
    location: "desktop",
    legacy_files: [],
  });
  const native = vi.fn(async ({ path, reveal }) =>
    reveal
      ? null
      : {
          root: "/local/thr_a",
          entries: path
            ? [{ name: "rain.txt", is_dir: false }]
            : [{ name: "output", is_dir: true }],
        },
  );
  Object.defineProperty(window, "__mossworkRuntime", {
    configurable: true,
    value: { threadWorkspace: native },
  });
  render(<ThreadWorkspacePanel {...props} />);
  fireEvent.click(await screen.findByText("output"));
  fireEvent.click(await screen.findByText("rain.txt"));
  expect(screen.queryByText("/local/thr_a")).not.toBeInTheDocument();
  expect(native).toHaveBeenLastCalledWith(
    expect.objectContaining({
      agent_id: "expert",
      thread_id: "thr_a",
      path: "output/rain.txt",
      reveal: true,
    }),
  );
  expect(props.onOpenFile).not.toHaveBeenCalled();
});

it("keeps sibling folders open independently and refreshes expanded branches", async () => {
  const user = userEvent.setup();
  let name = "before.txt";
  const list = vi
    .spyOn(mossworkThreadsApi, "workspace")
    .mockImplementation(async (_agent, _thread, path) => ({
      ...root,
      entries: path
        ? [{ name: path === "inputs" ? "input.txt" : name, is_dir: false }]
        : root.entries,
    }));
  render(<ThreadWorkspacePanel {...props} />);
  await user.click(await screen.findByText("generated"));
  await screen.findByText("before.txt");
  await user.click(screen.getByText("inputs"));
  await screen.findByText("input.txt");
  expect(screen.getByText("before.txt")).toBeVisible();
  await user.click(screen.getByText("generated"));
  expect(screen.queryByText("before.txt")).not.toBeInTheDocument();
  expect(screen.getByText("input.txt")).toBeVisible();
  await user.click(screen.getByText("generated"));
  await screen.findByText("before.txt");
  name = "after.txt";
  list.mockClear();
  await user.click(screen.getByRole("button", { name: /refresh|刷新/i }));
  await screen.findByText("after.txt");
  expect(await screen.findByText("input.txt")).toBeVisible();
  expect(screen.queryByText("before.txt")).not.toBeInTheDocument();
  expect(list.mock.calls.map((args) => args[2]).sort()).toEqual([
    "",
    "generated",
    "inputs",
  ]);
});
