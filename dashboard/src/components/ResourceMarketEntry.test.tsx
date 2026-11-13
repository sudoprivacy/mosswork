import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ResourceMarketEntry from "./ResourceMarketEntry";
import { localSkillMarketApi } from "../api/modules/localSkillMarket";
import { useCurrentUser } from "../hooks/useCurrentUser";

const translate = (key: string) => key;
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: translate }) }));
vi.mock("../hooks/useCurrentUser", () => ({ useCurrentUser: vi.fn() }));
vi.mock("../api/modules/localSkillMarket", () => ({
  localSkillMarketApi: { list: vi.fn(), detail: vi.fn(), install: vi.fn() },
}));
vi.mock("../utils/antdMessage", () => ({ message: { success: vi.fn() } }));
const release = {
  resource_id: "resource",
  version_id: "v1",
  version: "1.0.0",
  object_sha256: "a".repeat(64),
  name: "Offline inspection",
  description: "A local skill",
  dependencies: [],
};
const newer = { ...release, version_id: "v2", version: "2.0.0" };
const detail = {
  ...release,
  slug: "inspection",
  changelog: "Initial release",
  versions: [newer, release],
  lock: [
    {
      resource_id: "resource",
      version_id: "v1",
      version: "1.0.0",
      object_sha256: release.object_sha256,
    },
  ],
  file_contents: [
    { name: "skills/inspection/SKILL.md", content: "Inspection instructions" },
  ],
  runtime: {},
  configuration: {},
};
function show(props = {}) {
  return render(
    <MemoryRouter>
      <ResourceMarketEntry {...props} />
    </MemoryRouter>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useCurrentUser).mockReturnValue({
    id: 2,
    role: "user",
    permissions: ["skill_packages"],
  } as ReturnType<typeof useCurrentUser>);
  vi.mocked(localSkillMarketApi.list).mockResolvedValue({ items: [release] });
  vi.mocked(localSkillMarketApi.detail).mockResolvedValue(detail);
  vi.mocked(localSkillMarketApi.install).mockResolvedValue({
    package: { id: "package-v1", name: "Inspection v1", skill_count: 1 },
    source: detail,
  });
});

it("lets a consumer without publishing access inspect and install a fixed release", async () => {
  const onInstalled = vi.fn();
  show({ onInstalled });
  expect(await screen.findByText("Offline inspection")).toBeInTheDocument();
  expect(screen.queryByText("localSkillMarket.manage")).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "localSkillMarket.details" }),
  );
  expect(
    await screen.findByText("skills/inspection/SKILL.md"),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByText("skills/inspection/SKILL.md"));
  expect(
    await screen.findByText("Inspection instructions"),
  ).toBeInTheDocument();
  const name = screen.getByRole("textbox", {
    name: "localSkillMarket.packageName",
  });
  await userEvent.clear(name);
  await userEvent.type(name, "My offline skills");
  await userEvent.click(
    screen.getByRole("button", { name: "localSkillMarket.install" }),
  );
  await waitFor(() => {
    expect(localSkillMarketApi.install).toHaveBeenCalledWith(
      "v1",
      "My offline skills",
    );
    expect(onInstalled).toHaveBeenCalledWith("package-v1");
  });
});

it("installs the explicitly selected historical version", async () => {
  vi.mocked(localSkillMarketApi.detail).mockImplementation(async (id) =>
    id === "v2" ? { ...detail, ...newer } : detail,
  );
  show({ initialVersionId: "v2" });
  expect(
    await screen.findByDisplayValue("Offline inspection v2.0.0"),
  ).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("combobox", { name: "resourceMarket.version" }),
  );
  const choices = await screen.findAllByText("v1.0.0");
  await userEvent.click(choices[choices.length - 1]);
  expect(
    await screen.findByDisplayValue("Offline inspection v1.0.0"),
  ).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "localSkillMarket.install" }),
  );
  await waitFor(() =>
    expect(localSkillMarketApi.install).toHaveBeenCalledWith(
      "v1",
      "Offline inspection v1.0.0",
    ),
  );
});

it("retains the detail and package name when installation fails", async () => {
  const onInstalled = vi.fn();
  vi.mocked(localSkillMarketApi.install).mockRejectedValue(
    new Error("Name already exists"),
  );
  show({ initialVersionId: "v1", onInstalled });
  await screen.findByDisplayValue("Offline inspection v1.0.0");
  await userEvent.click(
    screen.getByRole("button", { name: "localSkillMarket.install" }),
  );
  expect(await screen.findByText("Name already exists")).toBeInTheDocument();
  expect(
    screen.getByDisplayValue("Offline inspection v1.0.0"),
  ).toBeInTheDocument();
  expect(onInstalled).not.toHaveBeenCalled();
});

it("reports catalog failures and supports refresh", async () => {
  vi.mocked(localSkillMarketApi.list).mockRejectedValueOnce(
    new Error("Unavailable"),
  );
  show();
  expect(await screen.findByText("Unavailable")).toBeInTheDocument();
  await userEvent.click(
    await screen.findByRole("button", { name: "common.refresh" }),
  );
  expect(await screen.findByText("Offline inspection")).toBeInTheDocument();
});

it("does not query the market without skill package access", () => {
  vi.mocked(useCurrentUser).mockReturnValue({
    id: 3,
    role: "user",
    permissions: [],
  } as unknown as ReturnType<typeof useCurrentUser>);
  show();
  expect(screen.getByText("resourceMarket.contactAdmin")).toBeInTheDocument();
  expect(localSkillMarketApi.list).not.toHaveBeenCalled();
});
