import { act, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

const loading = vi.hoisted(() => {
  let resolve!: () => void;
  let markRequested!: () => void;
  const pending = new Promise<void>((done) => {
    resolve = done;
  });
  const requested = new Promise<void>((done) => {
    markRequested = done;
  });
  return { pending, resolve, requested, markRequested };
});

vi.mock("./index", async (importOriginal) => {
  loading.markRequested();
  await loading.pending;
  return importOriginal<typeof import("./index")>();
});

import LazyMarkdown from "./LazyMarkdown";

// Compile the actual parser during module setup. The gate above still holds
// the real lazy wrapper pending until this case explicitly releases it.
await vi.importActual<typeof import("./index")>("./index");

it("shows the latest plain fallback until the real renderer is available", async () => {
  const view = render(<LazyMarkdown content={"## First\n\n**Old content**"} />);
  await act(async () => {
    await loading.requested;
  });
  expect(screen.getByText("## First **Old content**")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "First" })).toBeNull();

  await act(async () => {
    view.rerender(
      <LazyMarkdown content={"## Latest\n\n**Current content**"} />,
    );
  });
  expect(screen.getByText("## Latest **Current content**")).toBeInTheDocument();
  expect(screen.queryByText(/Old content/)).toBeNull();
  expect(screen.queryByRole("heading", { name: "Latest" })).toBeNull();

  await act(async () => {
    loading.resolve();
  });
  await waitFor(() => {
    expect(screen.getByRole("heading", { name: "Latest" })).toBeInTheDocument();
    expect(view.container.querySelector("strong")).toHaveTextContent(
      "Current content",
    );
    expect(view.container).not.toHaveTextContent("**Current content**");
    expect(screen.queryByRole("heading", { name: "First" })).toBeNull();
  });
});
