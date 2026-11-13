import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./config", () => ({ getApiUrl: (path: string) => `/api${path}` }));
vi.mock("../i18n", () => ({ default: { language: "en" } }));

describe("central canonical token pair", () => {
  let store: typeof import("./request");
  beforeEach(async () => {
    vi.resetModules();
    localStorage.clear();
    sessionStorage.clear();
    store = await import("./request");
  });
  it.each([true, false])(
    "publishes one complete pair, remember=%s",
    (remember) => {
      const observed: string[][] = [];
      const listener = () =>
        observed.push([store.getAuthToken(), store.getAuthRefreshToken()]);
      window.addEventListener(store.AUTH_TOKEN_CHANGED_EVENT, listener);
      store.setAuthTokens("access", "refresh", remember);
      expect(observed).toEqual([["access", "refresh"]]);
      expect(store.isSessionOnlyAuth()).toBe(!remember);
      window.removeEventListener(store.AUTH_TOKEN_CHANGED_EVENT, listener);
      store.clearAuthToken();
      expect(store.getAuthRefreshToken()).toBe("");
    },
  );
  it("never refreshes another access token and clears the pair for standalone login", () => {
    store.setAuthTokens("canonical", "canonical-refresh");
    localStorage.setItem("auth_token", "other");
    expect(store.getAuthRefreshToken()).toBe("");
    store.setAuthToken("standalone");
    expect(store.getAuthRefreshToken()).toBe("");
    expect(sessionStorage.getItem("auth_refresh_pair")).toBeNull();
    expect(localStorage.getItem("auth_refresh_pair")).toBeNull();
  });
  it("selects trusted status before an obsolete standalone setup latch", async () => {
    store.markSetupRequired();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            mode: "canonical",
            phone_login_available: false,
            password_login_available: true,
          }),
          { headers: { "content-type": "application/json" } },
        ),
      ),
    );
    const { canonicalIdentityApi } = await import(
      "./modules/canonicalIdentity"
    );
    expect((await canonicalIdentityApi.status()).mode).toBe("canonical");
    expect(fetch).toHaveBeenCalledWith(
      "/api/identity/status",
      expect.anything(),
    );
  });
  it.each([
    "phone/send-code",
    "phone/login",
    "refresh",
    "password/login",
    "moss/password/login",
  ])(
    "keeps public %s 401 envelopes without auth events or redirect",
    async (path) => {
      store.setAuthTokens("current", "current-refresh");
      const replace = vi.fn();
      Object.defineProperty(window, "location", {
        configurable: true,
        value: { pathname: "/login", replace },
      });
      const changed = vi.fn(),
        unauthorized = vi.fn();
      window.addEventListener(store.AUTH_TOKEN_CHANGED_EVENT, changed);
      window.addEventListener(store.UNAUTHORIZED_EVENT, unauthorized);
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
      await expect(
        store.request(`/identity/${path}`, { method: "POST" }),
      ).rejects.toThrow("PHONE_AUTH_FAILED");
      expect(changed).not.toHaveBeenCalled();
      expect(unauthorized).not.toHaveBeenCalled();
      expect(replace).not.toHaveBeenCalled();
      expect(store.getAuthToken()).toBe("current");
      expect(store.getAuthRefreshToken()).toBe("current-refresh");
      window.removeEventListener(store.AUTH_TOKEN_CHANGED_EVENT, changed);
      window.removeEventListener(store.UNAUTHORIZED_EVENT, unauthorized);
    },
  );
  it("uses separate typed canonical password endpoints", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({}), {
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { canonicalIdentityApi } = await import(
      "./modules/canonicalIdentity"
    );
    await canonicalIdentityApi.passwordLogin({
      email: "member@example.test",
      password: "public-fixture-password",
    });
    await canonicalIdentityApi.mossPasswordLogin({
      username: "home",
      password: "public-fixture-password",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/identity/password/login",
      expect.objectContaining({
        body: JSON.stringify({
          email: "member@example.test",
          password: "public-fixture-password",
        }),
        method: "POST",
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/identity/moss/password/login",
      expect.objectContaining({
        body: JSON.stringify({
          username: "home",
          password: "public-fixture-password",
        }),
        method: "POST",
      }),
    );
  });
  it("requires the trusted status to report password availability as a boolean", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            mode: "canonical",
            phone_login_available: true,
            password_login_available: "true",
          }),
          { headers: { "content-type": "application/json" } },
        ),
      ),
    );
    const { canonicalIdentityApi } = await import(
      "./modules/canonicalIdentity"
    );
    await expect(canonicalIdentityApi.status()).rejects.toThrow(
      "Invalid identity mode",
    );
  });
});
