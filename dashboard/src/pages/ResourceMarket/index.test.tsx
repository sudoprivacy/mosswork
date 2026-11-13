import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import type { ReactNode } from "react";
import ResourceMarketPage from "./index";
import {
  resourceMarketApi,
  type MarketDetail,
  type MarketDraft,
} from "../../api/modules/resourceMarket";
import { authApi } from "../../api/modules/auth";

function translate(key: string) {
  return key;
}
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: translate }) }));
vi.mock("../../layouts/PageShell", () => ({
  default: ({
    children,
    actions,
  }: {
    children: ReactNode;
    actions: ReactNode;
  }) => (
    <main>
      {actions}
      {children}
    </main>
  ),
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
vi.mock("../../api/modules/auth", () => ({ authApi: { me: vi.fn() } }));
vi.mock("../../api/modules/resourceMarket", () => ({
  resourceMarketApi: {
    list: vi.fn(),
    detail: vi.fn(),
    import: vi.fn(),
    submit: vi.fn(),
    review: vi.fn(),
    preview: vi.fn(),
    listing: vi.fn(),
    recommend: vi.fn(),
    download: vi.fn(),
    example: vi.fn(),
  },
}));

const manifest = {
  kind: "skill" as const,
  slug: "inspection",
  name: "Inspection skill",
  description: "Offline test",
  version: "1.0.0",
  changelog: "Initial release",
  dependencies: [],
  runtime: {
    execution_targets: ["server"],
    platforms: ["any"],
    python: "",
    runtime_profile: "",
  },
  configuration: {},
  license: "MIT",
  author: "",
};
const draft: MarketDraft = {
  draft_id: "draft1",
  resource_id: "resource1",
  version: "1.0.0",
  state: "draft",
  object_sha256: "a".repeat(64),
  manifest,
  files: [{ path: "SKILL.md", size: 10, sha256: "b".repeat(64) }],
  file_count: 2,
  total_bytes: 100,
  review_note: "",
};
let detail: MarketDetail;

const getComputedStyle = window.getComputedStyle;

beforeEach(() => {
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    getComputedStyle(element),
  );
  vi.clearAllMocks();
  detail = {
    resource: {
      resource_id: "resource1",
      slug: "inspection",
      kind: "skill",
      name: "Inspection skill",
      description: "Offline test",
      owner_user_id: 1,
      listed: true,
      recommended_version_id: null,
      recommended_version: null,
      version_count: 0,
    },
    drafts: [{ ...draft }],
    versions: [],
  };
  vi.mocked(authApi.me).mockResolvedValue({ id: 1, role: "admin" } as Awaited<
    ReturnType<typeof authApi.me>
  >);
  vi.mocked(resourceMarketApi.list).mockImplementation(async () =>
    detail.resource.version_count || detail.drafts[0].state !== "draft"
      ? [detail.resource]
      : [],
  );
  vi.mocked(resourceMarketApi.detail).mockImplementation(async () =>
    structuredClone(detail),
  );
  vi.mocked(resourceMarketApi.import).mockResolvedValue(draft);
  vi.mocked(resourceMarketApi.submit).mockImplementation(async () => {
    detail.drafts[0].state = "submitted";
    return { ...detail.drafts[0] };
  });
  vi.mocked(resourceMarketApi.review).mockImplementation(async () => {
    detail.drafts[0].state = "published";
    detail.resource.version_count = 1;
    detail.resource.recommended_version_id = "version1";
    detail.versions = [
      {
        version_id: "version1",
        resource_id: "resource1",
        version: "1.0.0",
        object_sha256: draft.object_sha256,
        manifest,
        published_at: 1_700_000_000,
      },
    ];
    return { ...detail.drafts[0] };
  });
});

describe("ResourceMarket", () => {
  it("connects offline import, draft submission, administrator review and release history", async () => {
    const user = userEvent.setup();
    render(
      <App>
        <ResourceMarketPage />
      </App>,
    );
    await screen.findByText("resourceMarket.empty");
    await user.click(
      screen.getByRole("button", { name: /resourceMarket.import$/ }),
    );
    const file = new File(["zip"], "inspection.zip", {
      type: "application/zip",
    });
    await user.upload(screen.getByLabelText("resourceMarket.chooseFile"), file);
    await user.click(
      screen.getByRole("button", { name: "resourceMarket.createDraft" }),
    );
    await screen.findByRole("button", { name: "resourceMarket.submit" });
    expect(resourceMarketApi.import).toHaveBeenCalledWith(file, undefined);
    await user.click(
      screen.getByRole("button", { name: "resourceMarket.submit" }),
    );
    await screen.findByRole("button", { name: "resourceMarket.publish" });
    await user.click(
      screen.getByRole("button", { name: "resourceMarket.publish" }),
    );
    await user.type(
      screen.getByLabelText("resourceMarket.reviewNote"),
      "Reviewed offline",
    );
    await user.click(
      screen.getByRole("button", { name: "resourceMarket.confirm" }),
    );
    await waitFor(() =>
      expect(resourceMarketApi.review).toHaveBeenCalledWith(
        expect.objectContaining({ object_sha256: draft.object_sha256 }),
        "publish",
        "Reviewed offline",
      ),
    );
    await user.click(
      screen.getByRole("tab", { name: "resourceMarket.versions" }),
    );
    await screen.findByRole("button", { name: /resourceMarket.download/ });
    expect(screen.getByText(/2023/)).toBeInTheDocument();
  }, 20000);

  it("lets developers submit their drafts while hiding administrator release actions", async () => {
    const user = userEvent.setup();
    detail.drafts[0].state = "submitted";
    vi.mocked(authApi.me).mockResolvedValue({ id: 1, role: "user" } as Awaited<
      ReturnType<typeof authApi.me>
    >);
    render(
      <App>
        <ResourceMarketPage />
      </App>,
    );
    await user.click(
      await screen.findByRole("button", { name: /Inspection skill/ }),
    );
    await user.click(
      await screen.findByRole("tab", { name: "resourceMarket.drafts" }),
    );
    await waitFor(() =>
      expect(
        screen.getAllByText("resourceMarket.states.submitted").length,
      ).toBeGreaterThan(0),
    );
    expect(
      screen.queryByRole("button", { name: "resourceMarket.publish" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "resourceMarket.reject" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "resourceMarket.unlist" }),
    ).not.toBeInTheDocument();
  });
});
