import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CanonicalMemberLogin } from "./CanonicalLogin";
import { clearSetupRequired, setAuthTokens } from "../../api/request";
import en from "../../locales/en.json";

vi.mock("react-i18next", async () => {
  const { default: catalog } = await import("../../locales/en.json");
  return {
    useTranslation: () => ({
      t: (key: string) =>
        key === "canonical.profileSaveUnconfirmed"
          ? catalog.canonical.profileSaveUnconfirmed
          : key,
    }),
  };
});
vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../../i18n", () => ({ default: { language: "en" } }));
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  clearSetupRequired();
  setAuthTokens("original-access", "original-refresh", false);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const identity = {
  account_id: "original-account",
  organization: { id: "original-org", name: "Company" },
  member: {
    id: "original-member",
    role: "user",
    status: "active",
    revision: 1,
  },
  moss_access: { is_enabled: false, is_granted: false, source: "explicit" },
  personal_agent_ref: null,
  can_switch_organization: false,
};
const profile = {
  account_id: "original-account",
  organization_id: "original-org",
  member_id: "original-member",
  login_name: "original-login",
  nickname: "Original nickname",
  avatar: null,
  can_change_password: true,
};

it("loads self-profile only on request and sends one original self nickname write", async () => {
  let nickname = profile.nickname;
  const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === "/api/identity/profile/nickname") {
      expect(new Headers(options?.headers).get("Authorization")).toBe(
        "Bearer original-access",
      );
      expect(JSON.parse(String(options?.body))).toEqual({
        nickname: "Current nickname",
      });
      nickname = "Current nickname";
      return new Response(JSON.stringify({ success: true }), {
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(
      JSON.stringify(
        url === "/api/identity/me" ? identity : { ...profile, nickname },
      ),
      { headers: { "content-type": "application/json" } },
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  render(
    <CanonicalMemberLogin phoneLoginAvailable={false} passwordLoginAvailable />,
  );
  await screen.findByText("canonical.runtimePending");
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/api/identity/me",
  ]);
  fireEvent.click(
    screen.getByRole("button", { name: "canonical.profileDetails" }),
  );
  const input = await screen.findByLabelText("canonical.nickname");
  fireEvent.change(input, { target: { value: "Current nickname" } });
  fireEvent.click(
    screen.getByRole("button", { name: "canonical.saveNickname" }),
  );
  await screen.findByText("canonical.profileSaved");
  await waitFor(() =>
    expect(screen.getByLabelText("canonical.nickname")).toHaveValue(
      "Current nickname",
    ),
  );
  expect(
    fetchMock.mock.calls.filter(
      ([url]) => url === "/api/identity/profile/nickname",
    ),
  ).toHaveLength(1);
});

it("withholds malformed write success and reloads without replaying the possible effect", async () => {
  let nickname = profile.nickname;
  let writes = 0;
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/identity/profile/nickname") {
      writes++;
      nickname = "Committed nickname";
      return new Response(JSON.stringify({ success: false }), {
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(
      JSON.stringify(
        url === "/api/identity/me" ? identity : { ...profile, nickname },
      ),
      { headers: { "content-type": "application/json" } },
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  render(
    <CanonicalMemberLogin phoneLoginAvailable={false} passwordLoginAvailable />,
  );
  await screen.findByText("canonical.runtimePending");
  fireEvent.click(
    screen.getByRole("button", { name: "canonical.profileDetails" }),
  );
  const input = await screen.findByLabelText("canonical.nickname");
  fireEvent.change(input, { target: { value: "  Committed nickname  " } });
  fireEvent.click(
    screen.getByRole("button", { name: "canonical.saveNickname" }),
  );
  await screen.findByText(en.canonical.profileSaveUnconfirmed);
  expect(screen.queryByText("canonical.profileSaved")).not.toBeInTheDocument();
  expect(writes).toBe(1);
  fireEvent.click(
    screen.getByRole("button", { name: "canonical.profileReload" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("canonical.nickname")).toHaveValue(
      "Committed nickname",
    ),
  );
  expect(
    fetchMock.mock.calls.filter(([url]) => url === "/api/identity/profile"),
  ).toHaveLength(2);
  expect(writes).toBe(1);
});
