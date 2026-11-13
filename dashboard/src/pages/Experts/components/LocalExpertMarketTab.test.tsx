import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LocalExpertMarketTab from "./LocalExpertMarketTab";
import { localExpertMarketApi } from "../../../api/modules/localExpertMarket";

const translate = (key: string) => key;
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: translate }) }));
vi.mock("../../../api/modules/localExpertMarket", () => ({
  localExpertMarketApi: { list: vi.fn(), detail: vi.fn() },
}));
const expert = {
  resource_id: "resource",
  version_id: "frozen-v1",
  version: "1.0.0",
  object_sha256: "a".repeat(64),
  name: "Inspection expert",
  description: "Offline inspection planning",
  dependencies: [{ resource_id: "skill", version: "1.0.0" }],
};
beforeEach(() => vi.clearAllMocks());

it("offers the exact released expert to the shared create drawer", async () => {
  vi.mocked(localExpertMarketApi.list).mockResolvedValue({
    items: [expert],
    can_create: true,
  });
  vi.mocked(localExpertMarketApi.detail).mockResolvedValue({
    ...expert,
    lock: [
      {
        resource_id: "resource",
        version: "1.0.0",
        version_id: "frozen-v1",
        object_sha256: "a".repeat(64),
      },
      {
        resource_id: "skill",
        version: "1.0.0",
        version_id: "skill-v1",
        object_sha256: "b".repeat(64),
      },
    ],
    file_contents: [{ name: "SOUL.md", content: "Inspection workflow" }],
  });
  const onCreate = vi.fn();
  render(<LocalExpertMarketTab onCreate={onCreate} />);
  expect(await screen.findByText("Inspection expert")).toBeInTheDocument();
  expect(screen.getByText("v1.0.0")).toBeInTheDocument();
  await userEvent.click(screen.getByText("Inspection expert"));
  expect(onCreate).toHaveBeenCalledWith(expert);
  await userEvent.click(
    screen.getByRole("button", { name: "experts.createFromTemplate" }),
  );
  expect(onCreate).toHaveBeenCalledTimes(2);
});

it("keeps assigned-only accounts in a read-only release detail", async () => {
  vi.mocked(localExpertMarketApi.list).mockResolvedValue({
    items: [expert],
    can_create: false,
  });
  vi.mocked(localExpertMarketApi.detail).mockResolvedValue({
    ...expert,
    lock: [
      {
        resource_id: "skill",
        version: "1.0.0",
        version_id: "skill-v1",
        object_sha256: "b".repeat(64),
      },
    ],
    file_contents: [{ name: "SOUL.md", content: "Inspection workflow" }],
  });
  render(<LocalExpertMarketTab onCreate={vi.fn()} />);
  await userEvent.click(await screen.findByText("Inspection expert"));
  expect(
    await screen.findByText("experts.localMarket.lockedDependencies"),
  ).toBeInTheDocument();
  expect(screen.getByText("skill · v1.0.0")).toBeInTheDocument();
  expect(screen.getByText("SOUL.md")).toBeInTheDocument();
  expect(localExpertMarketApi.detail).toHaveBeenCalledWith("frozen-v1");
});

it("keeps assigned-only accounts from opening arbitrary creation", async () => {
  vi.mocked(localExpertMarketApi.list).mockResolvedValue({
    items: [expert],
    can_create: false,
  });
  render(<LocalExpertMarketTab onCreate={vi.fn()} />);
  expect(
    await screen.findByRole("button", { name: "experts.createFromTemplate" }),
  ).toBeDisabled();
  expect(
    screen.getByText("experts.localMarket.assignedOnly"),
  ).toBeInTheDocument();
});

it("refreshes publication changes and reports failures instead of showing an empty market", async () => {
  vi.mocked(localExpertMarketApi.list)
    .mockRejectedValueOnce(new Error("market unavailable"))
    .mockResolvedValueOnce({ items: [expert], can_create: true });
  render(<LocalExpertMarketTab onCreate={vi.fn()} />);
  expect(await screen.findByText("market unavailable")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "common.refresh" }));
  await waitFor(() =>
    expect(screen.getByText("Inspection expert")).toBeInTheDocument(),
  );
});
