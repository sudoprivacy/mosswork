import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import CanonicalLogin from "./CanonicalLogin";
import {
  AUTH_TOKEN_CHANGED_EVENT,
  UNAUTHORIZED_EVENT,
  clearSetupRequired,
  getAuthToken,
  getAuthRefreshToken,
  isSessionOnlyAuth,
} from "../../api/request";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      key === "apiErrors.AUTH_FAILED"
        ? "Authentication failed."
        : key === "apiErrors.PHONE_AUTH_FAILED"
        ? "Unable to complete phone verification."
        : key,
  }),
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
it("shows an unavailable phone form without a local setup flow", () => {
  render(<CanonicalLogin phoneLoginAvailable={false} />);
  expect(screen.getByText("canonical.unavailable")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "canonical.signIn" }),
  ).toBeDisabled();
});
it("shows wrong-SMS errors without changing tokens or redirecting", async () => {
  const onAuthenticated = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "PHONE_AUTH_FAILED",
            message: "Unable to complete phone verification.",
          },
        }),
        { status: 401, headers: { "content-type": "application/json" } },
      ),
    ),
  );
  const changed = vi.fn(),
    unauthorized = vi.fn();
  window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, changed);
  window.addEventListener(UNAUTHORIZED_EVENT, unauthorized);
  render(
    <CanonicalLogin phoneLoginAvailable onAuthenticated={onAuthenticated} />,
  );
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("canonical.phone"), "13900000001");
  await user.type(screen.getByLabelText("canonical.code"), "123456");
  await user.click(screen.getByRole("button", { name: "canonical.signIn" }));
  expect(
    await screen.findByText("Unable to complete phone verification."),
  ).toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
  expect(unauthorized).not.toHaveBeenCalled();
  expect(onAuthenticated).not.toHaveBeenCalled();
  window.removeEventListener(AUTH_TOKEN_CHANGED_EVENT, changed);
  window.removeEventListener(UNAUTHORIZED_EVENT, unauthorized);
});
it("ignores a send-code response for an edited phone", async () => {
  let complete!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          complete = resolve;
        }),
    ),
  );
  render(<CanonicalLogin phoneLoginAvailable />);
  fireEvent.change(screen.getByLabelText("canonical.phone"), {
    target: { value: "13900000001" },
  });
  fireEvent.click(screen.getByRole("button", { name: "canonical.sendCode" }));
  fireEvent.change(screen.getByLabelText("canonical.phone"), {
    target: { value: "13900000002" },
  });
  complete(
    new Response(JSON.stringify({ next_send_in: 60, delivery: "tencent" }), {
      headers: { "content-type": "application/json" },
    }),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "canonical.sendCode" }),
    ).toBeEnabled(),
  );
  expect(screen.queryByText(/canonical.sendAgain/)).toBeNull();
});

it("uses the server cooldown for the current phone", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ next_send_in: 60, delivery: "tencent" }),
          { headers: { "content-type": "application/json" } },
        ),
      ),
  );
  render(<CanonicalLogin phoneLoginAvailable />);
  fireEvent.change(screen.getByLabelText("canonical.phone"), {
    target: { value: "13900000001" },
  });
  fireEvent.click(screen.getByRole("button", { name: "canonical.sendCode" }));
  expect(
    await screen.findByRole("button", { name: "canonical.sendAgain" }),
  ).toBeDisabled();
});

it("signs in with the central session-only pair and then displays the verified profile", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const data = url.endsWith("/phone/login")
        ? {
            access_token: "human-access",
            refresh_token: "human-refresh",
            token_type: "Bearer",
            expires_in: 300,
            account_id: "account-uuid",
            organization_id: "org-uuid",
            member_id: "member-uuid",
          }
        : url.endsWith("/me")
        ? {
            account_id: "account-uuid",
            organization: { id: "org-uuid", name: "Verified company" },
            member: {
              id: "member-uuid",
              role: "admin",
              status: "active",
              revision: 1,
            },
            moss_access: { is_enabled: true, is_granted: true, source: "role" },
            personal_agent_ref: null,
            can_switch_organization: true,
          }
        : { organizations: [] };
      return new Response(JSON.stringify(data), {
        headers: { "content-type": "application/json" },
      });
    }),
  );
  render(<CanonicalLogin phoneLoginAvailable />);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("canonical.phone"), "13900000001");
  await user.type(screen.getByLabelText("canonical.code"), "123456");
  await user.click(
    screen.getByRole("checkbox", { name: "canonical.remember" }),
  );
  await user.click(screen.getByRole("button", { name: "canonical.signIn" }));
  await screen.findByText("Verified company");
  expect(getAuthToken()).toBe("human-access");
  expect(getAuthRefreshToken()).toBe("human-refresh");
  expect(isSessionOnlyAuth()).toBe(true);
  expect(screen.getByText("canonical.runtimePending")).toBeInTheDocument();
});

it("shows both enabled methods on the same page and hides unavailable password entry", async () => {
  const view = render(
    <CanonicalLogin phoneLoginAvailable passwordLoginAvailable />,
  );
  expect(screen.getByLabelText("canonical.phone")).toBeInTheDocument();
  fireEvent.click(screen.getByText("canonical.passwordSignIn"));
  expect(screen.getByLabelText("canonical.username")).toBeInTheDocument();
  expect(screen.getByLabelText("canonical.password")).toBeInTheDocument();
  expect(screen.queryByLabelText("canonical.code")).toBeNull();
  fireEvent.click(screen.getByText("canonical.phoneSignIn"));
  expect(screen.getByLabelText("canonical.code")).toBeInTheDocument();
  view.rerender(
    <CanonicalLogin phoneLoginAvailable passwordLoginAvailable={false} />,
  );
  expect(screen.queryByText("canonical.passwordSignIn")).toBeNull();
});

it("signs in through the dedicated Moss password path without a forbidden organizations request", async () => {
  const fetchMock = vi.fn(async (url: string, _options?: RequestInit) => {
    if (url.endsWith("/organizations"))
      throw new Error("This credential cannot switch organizations");
    const data = url.endsWith("/moss/password/login")
      ? {
          access_token: "password-access",
          refresh_token: "password-refresh",
          token_type: "Bearer",
          expires_in: 300,
          account_id: "account-uuid",
          organization_id: "org-uuid",
          member_id: "member-uuid",
        }
      : {
          account_id: "account-uuid",
          organization: { id: "org-uuid", name: "Password company" },
          member: {
            id: "member-uuid",
            role: "admin",
            status: "active",
            revision: 1,
          },
          moss_access: { is_enabled: true, is_granted: true, source: "role" },
          personal_agent_ref: null,
          can_switch_organization: false,
        };
    return new Response(JSON.stringify(data), {
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<CanonicalLogin phoneLoginAvailable={false} passwordLoginAvailable />);
  expect(screen.queryByLabelText("canonical.phone")).toBeNull();
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("canonical.username"), "home");
  await user.type(
    screen.getByLabelText("canonical.password"),
    "public-password",
  );
  await user.click(
    screen.getByRole("checkbox", { name: "canonical.remember" }),
  );
  await user.click(screen.getByRole("button", { name: "canonical.signIn" }));
  await screen.findByText("canonical.account");
  expect(screen.getAllByText("Password company").length).toBeGreaterThan(0);
  expect(
    screen.getByRole("combobox", { name: "canonical.organization" }),
  ).toBeDisabled();
  expect(getAuthToken()).toBe("password-access");
  expect(getAuthRefreshToken()).toBe("password-refresh");
  expect(isSessionOnlyAuth()).toBe(true);
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/api/identity/moss/password/login",
    "/api/identity/me",
  ]);
  expect(fetchMock.mock.calls[0][1]).toMatchObject({
    body: JSON.stringify({ username: "home", password: "public-password" }),
  });
  expect(screen.getByText("canonical.runtimePending")).toBeInTheDocument();
});

it("shows wrong-password errors without token events or redirecting", async () => {
  const onAuthenticated = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: { code: "AUTH_FAILED", message: "Authentication failed." },
        }),
        { status: 401, headers: { "content-type": "application/json" } },
      ),
    ),
  );
  const changed = vi.fn(),
    unauthorized = vi.fn();
  window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, changed);
  window.addEventListener(UNAUTHORIZED_EVENT, unauthorized);
  render(
    <CanonicalLogin
      phoneLoginAvailable={false}
      passwordLoginAvailable
      onAuthenticated={onAuthenticated}
    />,
  );
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("canonical.username"), "home");
  await user.type(
    screen.getByLabelText("canonical.password"),
    "wrong-public-password",
  );
  await user.click(screen.getByRole("button", { name: "canonical.signIn" }));
  expect(await screen.findByText("Authentication failed.")).toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
  expect(unauthorized).not.toHaveBeenCalled();
  expect(onAuthenticated).not.toHaveBeenCalled();
  expect(getAuthToken()).toBe("");
  window.removeEventListener(AUTH_TOKEN_CHANGED_EVENT, changed);
  window.removeEventListener(UNAUTHORIZED_EVENT, unauthorized);
});
