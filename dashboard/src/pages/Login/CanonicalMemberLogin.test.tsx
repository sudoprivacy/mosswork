import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { CanonicalMemberLogin } from "./CanonicalLogin";
import {
  clearSetupRequired,
  getAuthRefreshToken,
  getAuthToken,
} from "../../api/request";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../../i18n", () => ({ default: { language: "en" } }));

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  clearSetupRequired();
});

it("uses ordinary organization password login without requiring personal Moss entitlement", async () => {
  const fetchMock = vi.fn(async (url: string) => {
    const result = url.endsWith("/password/login")
      ? {
          access_token: "ordinary-access",
          refresh_token: "ordinary-refresh",
          token_type: "Bearer",
          expires_in: 300,
          account_id: "original-account",
          organization_id: "original-company",
          member_id: "original-member",
        }
      : {
          account_id: "original-account",
          organization: { id: "original-company", name: "Company" },
          member: {
            id: "original-member",
            role: "user",
            status: "active",
            revision: 1,
          },
          moss_access: {
            is_enabled: false,
            is_granted: false,
            source: "explicit",
          },
          personal_agent_ref: null,
          can_switch_organization: false,
        };
    return new Response(JSON.stringify(result), {
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(
    <CanonicalMemberLogin phoneLoginAvailable={false} passwordLoginAvailable />,
  );
  fireEvent.change(screen.getByLabelText("canonical.username"), {
    target: { value: "ordinary" },
  });
  fireEvent.change(screen.getByLabelText("canonical.password"), {
    target: { value: "public-password" },
  });
  fireEvent.click(screen.getByRole("button", { name: "canonical.signIn" }));
  await waitFor(() => expect(getAuthToken()).toBe("ordinary-access"));
  expect(getAuthRefreshToken()).toBe("ordinary-refresh");
  await screen.findByText("canonical.runtimePending");
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/api/identity/password/login",
    "/api/identity/me",
  ]);
  expect(fetchMock.mock.calls.some(([url]) => url.includes("/moss/"))).toBe(
    false,
  );
  vi.unstubAllGlobals();
});
