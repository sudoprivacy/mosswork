import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import AuthGuard from "./AuthGuard";
import {
  canonicalIdentityApi,
  type CanonicalIdentity,
} from "../api/modules/canonicalIdentity";
import { authApi } from "../api/modules/auth";
import { setAuthToken } from "../api/request";

vi.mock("../api/modules/canonicalIdentity", () => ({
  canonicalIdentityApi: {
    status: vi.fn(),
    me: vi.fn(),
    organizations: vi.fn(),
    passwordLogin: vi.fn(),
  },
}));
vi.mock("../api/modules/auth", () => ({
  authApi: { getAuthStatus: vi.fn(), me: vi.fn() },
}));
vi.mock("../pages/Login/CanonicalLogin", () => ({
  CanonicalMemberLogin: () => <div>canonical-account</div>,
  CanonicalAccountScreen: () => <div>canonical-account</div>,
}));

const profile: CanonicalIdentity = {
  account_id: "original-human",
  organization: { id: "original-org", name: "Organization" },
  member: {
    id: "original-member",
    role: "member",
    status: "active",
    revision: 1,
  },
  moss_access: { is_enabled: false, is_granted: false, source: "explicit" },
  personal_agent_ref: null,
  can_switch_organization: false,
};

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}

function entry() {
  render(
    <MemoryRouter initialEntries={["/reviews/project/" + "a".repeat(32)]}>
      <AuthGuard canonicalReview>
        <div>human-private-page</div>
      </AuthGuard>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem("auth_token", "original-human-token");
  vi.mocked(canonicalIdentityApi.status).mockResolvedValue({
    mode: "canonical",
    phone_login_available: false,
    password_login_available: true,
  });
  vi.mocked(canonicalIdentityApi.me).mockResolvedValue(profile);
});

it("waits for a current canonical profile before displaying the human page", async () => {
  const checked = pending<CanonicalIdentity>();
  vi.mocked(canonicalIdentityApi.me).mockReturnValue(checked.promise);
  entry();
  await waitFor(() => expect(canonicalIdentityApi.me).toHaveBeenCalledTimes(1));
  expect(screen.queryByText("human-private-page")).toBeNull();
  await act(async () => checked.resolve(profile));
  await screen.findByText("human-private-page");
  expect(authApi.getAuthStatus).not.toHaveBeenCalled();
  expect(authApi.me).not.toHaveBeenCalled();
  expect(canonicalIdentityApi.organizations).not.toHaveBeenCalled();
});

it("uses the existing canonical login when credentials are absent", async () => {
  localStorage.clear();
  entry();
  await screen.findByText("canonical-account");
  expect(screen.queryByText("human-private-page")).toBeNull();
  expect(canonicalIdentityApi.me).not.toHaveBeenCalled();
  expect(authApi.getAuthStatus).not.toHaveBeenCalled();
});

it("withholds the page after canonical profile refusal", async () => {
  vi.mocked(canonicalIdentityApi.me).mockRejectedValue(new Error("refused"));
  entry();
  await screen.findByText("canonical-account");
  expect(screen.queryByText("human-private-page")).toBeNull();
  expect(authApi.me).not.toHaveBeenCalled();
});

it("withdraws visible content on credential change until the next profile passes", async () => {
  entry();
  await screen.findByText("human-private-page");
  const next = pending<CanonicalIdentity>();
  vi.mocked(canonicalIdentityApi.me).mockReturnValue(next.promise);
  act(() => setAuthToken("next-human-token"));
  expect(screen.queryByText("human-private-page")).toBeNull();
  await waitFor(() => expect(canonicalIdentityApi.me).toHaveBeenCalledTimes(2));
  await act(async () => next.resolve(profile));
  await screen.findByText("human-private-page");
});

it("refuses a standalone mode without local-user authentication fallback", async () => {
  vi.mocked(canonicalIdentityApi.status).mockResolvedValue({
    mode: "standalone",
    phone_login_available: false,
    password_login_available: false,
  });
  entry();
  await screen.findByRole("alert");
  expect(screen.queryByText("human-private-page")).toBeNull();
  expect(canonicalIdentityApi.me).not.toHaveBeenCalled();
  expect(authApi.getAuthStatus).not.toHaveBeenCalled();
});
