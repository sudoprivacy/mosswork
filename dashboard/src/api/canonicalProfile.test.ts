import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { canonicalIdentityApi } from "./modules/canonicalIdentity";
import { clearSetupRequired } from "./request";

vi.mock("./config", () => ({ getApiUrl: (path: string) => `/api${path}` }));
vi.mock("../i18n", () => ({ default: { language: "en" } }));
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  clearSetupRequired();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

it("uses only the original self nickname endpoint and captured bearer", async () => {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify({ success: true }), {
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  expect(
    await canonicalIdentityApi.updateNickname(
      "Current nickname",
      "original-access",
    ),
  ).toEqual({ success: true });
  const [url, options] = fetchMock.mock.calls[0] as unknown as [
    string,
    RequestInit,
  ];
  expect(url).toBe("/api/identity/profile/nickname");
  expect(new Headers(options.headers).get("Authorization")).toBe(
    "Bearer original-access",
  );
  expect(JSON.parse(String(options.body))).toEqual({
    nickname: "Current nickname",
  });
});

it("does not confirm or replay a nickname write after a malformed non-error reply", async () => {
  let effects = 0;
  const fetchMock = vi.fn(async () => {
    effects++;
    return new Response(JSON.stringify({ success: false }), {
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    canonicalIdentityApi.updateNickname(
      "Committed nickname",
      "original-access",
    ),
  ).rejects.toThrow("canonical.profileSaveUnconfirmed");
  expect(effects).toBe(1);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("withholds an invalid normalized avatar reply after one original POST", async () => {
  const fetchMock = vi.fn(
    async () =>
      new Response(
        JSON.stringify({ avatar: "https://foreign.invalid/image" }),
        { headers: { "content-type": "application/json" } },
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    canonicalIdentityApi.updateAvatar(
      "data:image/png;base64,eA==",
      "original-access",
    ),
  ).rejects.toThrow("canonical.profileSaveUnconfirmed");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("preserves the exact explicit avatar-clear operation", async () => {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify({ avatar: null }), {
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  expect(
    await canonicalIdentityApi.updateAvatar(null, "original-access"),
  ).toEqual({ avatar: null });
  const [url, options] = fetchMock.mock.calls[0] as unknown as [
    string,
    RequestInit,
  ];
  expect(url).toBe("/api/identity/profile/avatar");
  expect(JSON.parse(String(options.body))).toEqual({ avatar: null });
});

it("uses the unchanged original password field names without choosing a target member", async () => {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify({ success: true }), {
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  await canonicalIdentityApi.changePassword(
    "Old-fixture-password1",
    "New-fixture-password2",
    "original-access",
  );
  const [url, options] = fetchMock.mock.calls[0] as unknown as [
    string,
    RequestInit,
  ];
  expect(url).toBe("/api/identity/password/change");
  expect(JSON.parse(String(options.body))).toEqual({
    oldPassword: "Old-fixture-password1",
    newPassword: "New-fixture-password2",
  });
});
