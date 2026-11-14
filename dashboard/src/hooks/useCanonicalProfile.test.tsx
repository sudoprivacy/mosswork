import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  canonicalIdentityApi,
  type CanonicalIdentity,
  type CanonicalProfile,
} from "../api/modules/canonicalIdentity";
import {
  clearSetupRequired,
  getAuthToken,
  setAuthTokens,
} from "../api/request";
import { useCanonicalProfile } from "./useCanonicalProfile";

vi.mock("../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../i18n", () => ({ default: { language: "en" } }));

const actor: CanonicalIdentity = {
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
const profile: CanonicalProfile = {
  account_id: actor.account_id,
  organization_id: actor.organization.id,
  member_id: actor.member.id,
  login_name: "original-login",
  nickname: "Original nickname",
  avatar: null,
  can_change_password: true,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  clearSetupRequired();
  setAuthTokens("original-access", "original-refresh", false);
  vi.spyOn(canonicalIdentityApi, "profile").mockResolvedValue(profile);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("reads the exact original account/org/member using the held access token", async () => {
  const view = renderHook(() => useCanonicalProfile(actor));
  await waitFor(() => expect(view.result.current.profile).toEqual(profile));
  expect(canonicalIdentityApi.profile).toHaveBeenCalledWith(
    "original-access",
    expect.any(AbortSignal),
  );
});

it("withholds a foreign profile instead of displaying or mutating another member", async () => {
  vi.mocked(canonicalIdentityApi.profile).mockResolvedValue({
    ...profile,
    member_id: "foreign-member",
  });
  const write = vi
    .spyOn(canonicalIdentityApi, "updateNickname")
    .mockResolvedValue({ success: true });
  const view = renderHook(() => useCanonicalProfile(actor));
  await waitFor(() => expect(view.result.current.error).toBeInstanceOf(Error));
  expect(view.result.current.profile).toBeNull();
  await act(async () => {
    expect(await view.result.current.saveNickname("Changed")).toBe(false);
  });
  expect(write).not.toHaveBeenCalled();
});

it("refuses a token replacement even before React receives a changed actor", async () => {
  const write = vi
    .spyOn(canonicalIdentityApi, "updateNickname")
    .mockResolvedValue({ success: true });
  const view = renderHook(() => useCanonicalProfile(actor));
  await waitFor(() => expect(view.result.current.profile).toEqual(profile));
  setAuthTokens("different-access", "different-refresh", false);
  await act(async () => {
    expect(await view.result.current.saveNickname("Changed")).toBe(false);
  });
  expect(write).not.toHaveBeenCalled();
});

it("discards an older profile read after an organization change", async () => {
  const old = deferred<CanonicalProfile>();
  const changed = {
    ...actor,
    organization: { id: "other-org", name: "Other company" },
    member: { ...actor.member, id: "other-member" },
  };
  const current = {
    ...profile,
    organization_id: "other-org",
    member_id: "other-member",
    nickname: "Current nickname",
  };
  vi.mocked(canonicalIdentityApi.profile).mockImplementation(async (token) =>
    token === "original-access" ? old.promise : current,
  );
  const view = renderHook(({ identity }) => useCanonicalProfile(identity), {
    initialProps: { identity: actor },
  });
  await waitFor(() =>
    expect(canonicalIdentityApi.profile).toHaveBeenCalledTimes(1),
  );
  setAuthTokens("current-access", "current-refresh", false);
  view.rerender({ identity: changed });
  await waitFor(() => expect(view.result.current.profile).toEqual(current));
  await act(async () => {
    old.resolve(profile);
  });
  expect(view.result.current.profile).toEqual(current);
});

it("checks the original credential again after actual asynchronous file reading", async () => {
  let complete!: () => void;
  class DelayedFileReader {
    result: string | null = null;
    onerror: (() => void) | null = null;
    onload: (() => void) | null = null;
    readAsDataURL() {
      complete = () => {
        this.result = "data:image/png;base64,eA==";
        this.onload?.();
      };
    }
  }
  vi.stubGlobal("FileReader", DelayedFileReader);
  const write = vi
    .spyOn(canonicalIdentityApi, "updateAvatar")
    .mockResolvedValue({ avatar: "data:image/webp;base64,eA==" });
  const view = renderHook(() => useCanonicalProfile(actor));
  await waitFor(() => expect(view.result.current.profile).toEqual(profile));
  let result!: Promise<boolean>;
  act(() => {
    result = view.result.current.saveAvatar(
      new File(["x"], "avatar.png", { type: "image/png" }),
    );
  });
  await waitFor(() => expect(complete).toBeTypeOf("function"));
  setAuthTokens("new-actor-access", "new-actor-refresh", false);
  await act(async () => {
    complete();
    expect(await result).toBe(false);
  });
  expect(write).not.toHaveBeenCalled();
});

it("never retries a write after a possible commit and server refusal", async () => {
  let effects = 0;
  const write = vi
    .spyOn(canonicalIdentityApi, "updateNickname")
    .mockImplementation(async () => {
      effects++;
      throw new Error("Request failed: 503");
    });
  const view = renderHook(() => useCanonicalProfile(actor));
  await waitFor(() => expect(view.result.current.profile).toEqual(profile));
  await act(async () => {
    expect(await view.result.current.saveNickname("Committed nickname")).toBe(
      false,
    );
  });
  expect(effects).toBe(1);
  expect(write).toHaveBeenCalledTimes(1);
  expect(view.result.current.error).toBeInstanceOf(Error);
  expect(canonicalIdentityApi.profile).toHaveBeenCalledTimes(1);
  expect(view.result.current.profile).toBeNull();
  await act(async () => {
    expect(await view.result.current.saveNickname("Committed nickname")).toBe(
      false,
    );
  });
  expect(write).toHaveBeenCalledTimes(1);
  vi.mocked(canonicalIdentityApi.profile).mockResolvedValue({
    ...profile,
    nickname: "Committed nickname",
  });
  await act(async () => {
    await view.result.current.reload();
  });
  expect(view.result.current.profile?.nickname).toBe("Committed nickname");
  expect(write).toHaveBeenCalledTimes(1);
});

it("rejects an oversized local image before any avatar POST", async () => {
  const write = vi
    .spyOn(canonicalIdentityApi, "updateAvatar")
    .mockResolvedValue({ avatar: null });
  const view = renderHook(() => useCanonicalProfile(actor));
  await waitFor(() => expect(view.result.current.profile).toEqual(profile));
  await act(async () => {
    expect(
      await view.result.current.saveAvatar(
        new File([new Uint8Array(2 * 1024 * 1024 + 1)], "large.png"),
      ),
    ).toBe(false);
  });
  expect(write).not.toHaveBeenCalled();
});

it("does not replace or revoke the existing login after a confirmed own-password write", async () => {
  const write = vi
    .spyOn(canonicalIdentityApi, "changePassword")
    .mockResolvedValue({ success: true });
  const view = renderHook(() => useCanonicalProfile(actor));
  await waitFor(() => expect(view.result.current.profile).toEqual(profile));
  await act(async () => {
    expect(
      await view.result.current.changePassword(
        "Old-fixture-password1",
        "New-fixture-password2",
      ),
    ).toBe(true);
  });
  expect(write).toHaveBeenCalledWith(
    "Old-fixture-password1",
    "New-fixture-password2",
    "original-access",
  );
  expect(getAuthToken()).toBe("original-access");
});

it("keeps one original write when the user submits twice before React updates", async () => {
  const reply = deferred<{ success: true }>();
  const write = vi
    .spyOn(canonicalIdentityApi, "updateNickname")
    .mockReturnValue(reply.promise);
  const view = renderHook(() => useCanonicalProfile(actor));
  await waitFor(() => expect(view.result.current.profile).toEqual(profile));
  let first!: Promise<boolean>;
  let duplicate!: Promise<boolean>;
  act(() => {
    first = view.result.current.saveNickname("Changed nickname");
    duplicate = view.result.current.saveNickname("Changed nickname");
  });
  expect(await duplicate).toBe(false);
  expect(write).toHaveBeenCalledTimes(1);
  await act(async () => {
    reply.resolve({ success: true });
    expect(await first).toBe(true);
  });
  expect(write).toHaveBeenCalledTimes(1);
});

it("withholds the saved confirmation when the post-write current read fails", async () => {
  const write = vi
    .spyOn(canonicalIdentityApi, "updateNickname")
    .mockResolvedValue({ success: true });
  const view = renderHook(() => useCanonicalProfile(actor));
  await waitFor(() => expect(view.result.current.profile).toEqual(profile));
  vi.mocked(canonicalIdentityApi.profile).mockRejectedValue(
    new Error("current read unavailable"),
  );
  await act(async () => {
    expect(await view.result.current.saveNickname("Committed nickname")).toBe(
      false,
    );
  });
  expect(write).toHaveBeenCalledTimes(1);
  expect(view.result.current.error).toBeInstanceOf(Error);
  expect(view.result.current.profile).toBeNull();
  expect(view.result.current.isPending).toBe(false);
});
