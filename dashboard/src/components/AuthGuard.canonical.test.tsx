import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import AuthGuard from "./AuthGuard";
import LoginPage from "../pages/Login";
import { canonicalIdentityApi } from "../api/modules/canonicalIdentity";
import { authApi } from "../api/modules/auth";

const canonicalLogin = vi.hoisted(() => vi.fn());

vi.mock("../api/modules/canonicalIdentity", () => ({
  canonicalIdentityApi: { status: vi.fn() },
}));
vi.mock("../api/modules/auth", () => ({
  authApi: { getAuthStatus: vi.fn(), me: vi.fn() },
}));
vi.mock("../pages/Login/CanonicalLogin", () => ({
  default: (props: {
    phoneLoginAvailable: boolean;
    passwordLoginAvailable: boolean;
  }) => {
    canonicalLogin(props);
    return <div>canonical-account</div>;
  },
  CanonicalMemberLogin: (props: {
    phoneLoginAvailable: boolean;
    passwordLoginAvailable: boolean;
  }) => {
    canonicalLogin(props);
    return <div>canonical-account</div>;
  },
}));

beforeEach(() => {
  vi.resetAllMocks();
});
it("selects canonical account shell without integer runtime children or local setup", async () => {
  vi.mocked(canonicalIdentityApi.status).mockResolvedValue({
    mode: "canonical",
    phone_login_available: true,
    password_login_available: true,
  });
  render(
    <MemoryRouter>
      <AuthGuard>
        <div>integer-runtime</div>
      </AuthGuard>
    </MemoryRouter>,
  );
  await screen.findByText("canonical-account");
  expect(screen.queryByText("integer-runtime")).toBeNull();
  expect(authApi.getAuthStatus).not.toHaveBeenCalled();
  expect(authApi.me).not.toHaveBeenCalled();
  expect(canonicalLogin).toHaveBeenCalledWith(
    expect.objectContaining({
      phoneLoginAvailable: true,
      passwordLoginAvailable: true,
    }),
  );
});
it("direct login uses the same trusted canonical mode before probing setup", async () => {
  vi.mocked(canonicalIdentityApi.status).mockResolvedValue({
    mode: "canonical",
    phone_login_available: false,
    password_login_available: true,
  });
  render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  );
  await screen.findByText("canonical-account");
  expect(authApi.getAuthStatus).not.toHaveBeenCalled();
  expect(canonicalLogin).toHaveBeenCalledWith(
    expect.objectContaining({
      phoneLoginAvailable: false,
      passwordLoginAvailable: true,
    }),
  );
});
it("fails closed on unavailable mode and never guesses standalone", async () => {
  vi.mocked(canonicalIdentityApi.status).mockRejectedValue(
    new TypeError("Failed to fetch"),
  );
  render(
    <MemoryRouter>
      <AuthGuard>
        <div>integer-runtime</div>
      </AuthGuard>
    </MemoryRouter>,
  );
  await screen.findByRole("alert");
  expect(screen.queryByText("integer-runtime")).toBeNull();
  expect(authApi.getAuthStatus).not.toHaveBeenCalled();
});
