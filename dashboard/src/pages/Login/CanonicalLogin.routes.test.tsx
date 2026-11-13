import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import LoginPage, { CanonicalMossLoginPage } from "./index";
import { clearSetupRequired, getAuthToken } from "../../api/request";

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
afterEach(() => {
  vi.unstubAllGlobals();
});

function routes(entry: string, ordinary = <LoginPage />) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/login" element={ordinary} />
        <Route path="/login/moss" element={<CanonicalMossLoginPage />} />
        <Route
          path="/"
          element={<div>authenticated-account-destination</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

it("makes the dedicated realm reachable from the ordinary login without changing its endpoint", async () => {
  const fetchMock = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          mode: "canonical",
          phone_login_available: false,
          password_login_available: true,
        }),
        { headers: { "content-type": "application/json" } },
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
  routes("/login");
  const link = await screen.findByRole("link", {
    name: "canonical.mossSignIn",
  });
  expect(link).toHaveAttribute("href", "/login/moss");
  expect(fetchMock.mock.calls).toHaveLength(1);
});

it("uses the original dedicated Moss password endpoint through its actual page route", async () => {
  const fetchMock = vi.fn(
    async (url: string) =>
      new Response(
        JSON.stringify(
          url === "/api/identity/status"
            ? {
                mode: "canonical",
                phone_login_available: false,
                password_login_available: true,
              }
            : url === "/api/identity/moss/password/login"
            ? {
                access_token: "moss-access",
                refresh_token: "moss-refresh",
                token_type: "Bearer",
                expires_in: 300,
                account_id: "original-account",
                organization_id: "original-org",
                member_id: "original-member",
              }
            : {
                account_id: "original-account",
                organization: { id: "original-org", name: "Company" },
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
              },
        ),
        { headers: { "content-type": "application/json" } },
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
  routes("/login/moss");
  fireEvent.change(await screen.findByLabelText("canonical.username"), {
    target: { value: "original-member" },
  });
  fireEvent.change(screen.getByLabelText("canonical.password"), {
    target: { value: "Fixture-password1" },
  });
  fireEvent.click(screen.getByRole("button", { name: "canonical.signIn" }));
  await waitFor(() => expect(getAuthToken()).toBe("moss-access"));
  await screen.findByText("authenticated-account-destination");
  const urls = fetchMock.mock.calls.map(([url]) => url);
  expect(
    urls.filter((url) => url === "/api/identity/moss/password/login"),
  ).toHaveLength(1);
  expect(urls).not.toContain("/api/identity/password/login");
});

it("returns an unconfigured standalone deployment to its existing login route", async () => {
  const fetchMock = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          mode: "standalone",
          phone_login_available: false,
          password_login_available: false,
        }),
        { headers: { "content-type": "application/json" } },
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
  routes("/login/moss", <div>existing-standalone-login</div>);
  await screen.findByText("existing-standalone-login");
  expect(fetchMock.mock.calls).toHaveLength(1);
});
