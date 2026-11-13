import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useLayoutEffect, useRef } from "react";
import type { CanonicalIdentity } from "../api/modules/canonicalIdentity";
import {
  canonicalOrganizationApi,
  type CanonicalDepartmentDetails,
  type CanonicalOrganizationMember,
} from "../api/modules/canonicalOrganization";
import { clearSetupRequired, setAuthTokens } from "../api/request";
import {
  showsOrganizationDirectory,
  useCanonicalOrganization,
} from "./useCanonicalOrganization";

vi.mock("../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../i18n", () => ({ default: { language: "en" } }));
const identity: CanonicalIdentity = {
  account_id: "original-account",
  organization: { id: "original-org", name: "Company" },
  member: {
    id: "original-member",
    role: "admin",
    status: "active",
    revision: 1,
  },
  moss_access: { is_enabled: false, is_granted: false, source: "explicit" },
  personal_agent_ref: null,
  can_switch_organization: true,
};
const member: CanonicalOrganizationMember = {
  account_id: identity.account_id,
  organization_id: identity.organization.id,
  member_id: identity.member.id,
  username: "original-login",
  display_name: "Original admin",
  role: "admin",
  status: "active",
  membership_revision: 1,
  created_at: 1234,
  legacy_id: 42,
  department_id: null,
};
const department: CanonicalDepartmentDetails = {
  department_id: "original-department",
  organization_id: identity.organization.id,
  name: "Original department",
  parent_id: null,
  created_at: 1234,
  updated_at: 1235,
  user_count: 0,
  external_id: null,
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
async function loaded() {
  const view = renderHook(() => useCanonicalOrganization(identity));
  await waitFor(() =>
    expect(view.result.current.data?.members).toEqual([member]),
  );
  return view;
}
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  clearSetupRequired();
  setAuthTokens("original-access", "original-refresh", false);
  vi.spyOn(canonicalOrganizationApi, "members").mockResolvedValue([member]);
  vi.spyOn(canonicalOrganizationApi, "departments").mockResolvedValue([
    department,
  ]);
});
afterEach(() => vi.restoreAllMocks());

it("loads the original current organization with the held human credential and no Moss entitlement condition", async () => {
  const view = await loaded();
  expect(showsOrganizationDirectory(identity.member.role)).toBe(true);
  expect(identity.moss_access.is_granted).toBe(false);
  expect(canonicalOrganizationApi.members).toHaveBeenCalledWith(
    "original-access",
    expect.any(AbortSignal),
  );
  expect(canonicalOrganizationApi.departments).toHaveBeenCalledWith(
    "original-access",
    expect.any(AbortSignal),
  );
  expect(view.result.current.data?.departments).toEqual([department]);
});

it("withholds the entire directory when a reply contains a foreign organization", async () => {
  vi.mocked(canonicalOrganizationApi.departments).mockResolvedValue([
    { ...department, organization_id: "foreign-org" },
  ]);
  const write = vi
    .spyOn(canonicalOrganizationApi, "setMemberPassword")
    .mockResolvedValue();
  const view = renderHook(() => useCanonicalOrganization(identity));
  await waitFor(() => expect(view.result.current.error).toBeInstanceOf(Error));
  expect(view.result.current.data).toBeNull();
  await act(async () => {
    expect(
      await view.result.current.setMemberPassword(
        member.member_id,
        "replacement",
      ),
    ).toBe(false);
  });
  expect(write).not.toHaveBeenCalled();
});

it("cannot substitute a local integer alias or an unlisted department for original IDs", async () => {
  const password = vi
    .spyOn(canonicalOrganizationApi, "setMemberPassword")
    .mockResolvedValue();
  const remove = vi
    .spyOn(canonicalOrganizationApi, "deleteDepartment")
    .mockResolvedValue();
  const view = await loaded();
  await act(async () => {
    expect(
      await view.result.current.setMemberPassword("42", "replacement"),
    ).toBe(false);
    expect(
      await view.result.current.deleteDepartment("unlisted-department"),
    ).toBe(false);
  });
  expect(password).not.toHaveBeenCalled();
  expect(remove).not.toHaveBeenCalled();
});

it("refuses a credential replacement before React can reuse the old loaded directory", async () => {
  const write = vi
    .spyOn(canonicalOrganizationApi, "createDepartment")
    .mockResolvedValue(department);
  const view = await loaded();
  setAuthTokens("other-access", "other-refresh", false);
  await act(async () => {
    expect(
      await view.result.current.createDepartment({
        name: "Changed",
        parent_id: null,
      }),
    ).toBe(false);
  });
  expect(write).not.toHaveBeenCalled();
});

it("refuses the old directory during an actor change before passive read cleanup runs", async () => {
  const write = vi
    .spyOn(canonicalOrganizationApi, "setMemberPassword")
    .mockResolvedValue();
  const changed = {
    ...identity,
    member: { ...identity.member, id: "new-member" },
  };
  const current = { ...member, member_id: "new-member" };
  let outcome: Promise<boolean> | undefined;
  const view = renderHook(
    ({ actor, attempt }) => {
      const state = useCanonicalOrganization(actor);
      const current = useRef(state);
      current.current = state;
      useLayoutEffect(() => {
        if (attempt)
          outcome = current.current.setMemberPassword(
            member.member_id,
            "replacement",
          );
      }, [attempt]);
      return state;
    },
    { initialProps: { actor: identity, attempt: false } },
  );
  await waitFor(() =>
    expect(view.result.current.data?.members).toEqual([member]),
  );
  vi.mocked(canonicalOrganizationApi.members).mockResolvedValue([current]);
  view.rerender({ actor: changed, attempt: true });
  await act(async () => {
    expect(await outcome).toBe(false);
  });
  expect(write).not.toHaveBeenCalled();
  await waitFor(() =>
    expect(view.result.current.data?.members).toEqual([current]),
  );
});

it("discards old organization reads after the actor switches organizations", async () => {
  const old = deferred<CanonicalOrganizationMember[]>();
  const changed: CanonicalIdentity = {
    ...identity,
    organization: { id: "new-org", name: "Other" },
    member: { ...identity.member, id: "new-member" },
  };
  const current = {
    ...member,
    organization_id: "new-org",
    member_id: "new-member",
  };
  vi.mocked(canonicalOrganizationApi.members).mockImplementation(
    async (token) => (token === "original-access" ? old.promise : [current]),
  );
  vi.mocked(canonicalOrganizationApi.departments).mockImplementation(
    async (token) => (token === "original-access" ? [department] : []),
  );
  const view = renderHook(({ actor }) => useCanonicalOrganization(actor), {
    initialProps: { actor: identity },
  });
  await waitFor(() =>
    expect(canonicalOrganizationApi.members).toHaveBeenCalledTimes(1),
  );
  setAuthTokens("new-access", "new-refresh", false);
  view.rerender({ actor: changed });
  await waitFor(() =>
    expect(view.result.current.data?.members).toEqual([current]),
  );
  await act(async () => {
    old.resolve([member]);
  });
  expect(view.result.current.data?.members).toEqual([current]);
});

it("admits one original department write when a submit is repeated before React updates", async () => {
  const reply = deferred<CanonicalDepartmentDetails>();
  const write = vi
    .spyOn(canonicalOrganizationApi, "createDepartment")
    .mockReturnValue(reply.promise);
  const view = await loaded();
  let first!: Promise<boolean>;
  let duplicate!: Promise<boolean>;
  act(() => {
    first = view.result.current.createDepartment({
      name: "Changed",
      parent_id: null,
    });
    duplicate = view.result.current.createDepartment({
      name: "Changed",
      parent_id: null,
    });
  });
  expect(await duplicate).toBe(false);
  expect(write).toHaveBeenCalledTimes(1);
  await act(async () => {
    reply.resolve(department);
    expect(await first).toBe(true);
  });
  expect(write).toHaveBeenCalledTimes(1);
});

it("keeps a possible commit after refusal and requires an explicit read before another write", async () => {
  let effects = 0;
  const committed = {
    ...department,
    department_id: "retained-new-department",
    name: "Committed",
  };
  const write = vi
    .spyOn(canonicalOrganizationApi, "createDepartment")
    .mockImplementation(async () => {
      effects++;
      vi.mocked(canonicalOrganizationApi.departments).mockResolvedValue([
        department,
        committed,
      ]);
      throw new Error("Request failed: 503");
    });
  const view = await loaded();
  await act(async () => {
    expect(
      await view.result.current.createDepartment({
        name: "Committed",
        parent_id: null,
      }),
    ).toBe(false);
  });
  expect(view.result.current.writeUnconfirmed).toBe(true);
  expect(view.result.current.data).toBeNull();
  await act(async () => {
    expect(
      await view.result.current.createDepartment({
        name: "Committed",
        parent_id: null,
      }),
    ).toBe(false);
  });
  expect(effects).toBe(1);
  await act(async () => {
    await view.result.current.reload();
  });
  expect(view.result.current.data?.departments).toEqual([
    department,
    committed,
  ]);
  expect(write).toHaveBeenCalledTimes(1);
});

it("withholds an apply reply bound to a different original department", async () => {
  const write = vi
    .spyOn(canonicalOrganizationApi, "updateDepartment")
    .mockResolvedValue({
      ...department,
      department_id: "different-department",
    });
  const view = await loaded();
  await act(async () => {
    expect(
      await view.result.current.updateDepartment(department.department_id, {
        name: "Changed",
      }),
    ).toBe(false);
  });
  expect(view.result.current.writeUnconfirmed).toBe(true);
  expect(view.result.current.data).toBeNull();
  expect(write).toHaveBeenCalledTimes(1);
  expect(canonicalOrganizationApi.members).toHaveBeenCalledTimes(1);
});

it("withholds saved confirmation when the current read fails after a successful original write", async () => {
  const write = vi
    .spyOn(canonicalOrganizationApi, "setMemberPassword")
    .mockResolvedValue();
  const view = await loaded();
  vi.mocked(canonicalOrganizationApi.members).mockRejectedValue(
    new Error("current directory unavailable"),
  );
  await act(async () => {
    expect(
      await view.result.current.setMemberPassword(
        member.member_id,
        "replacement",
      ),
    ).toBe(false);
  });
  expect(write).toHaveBeenCalledTimes(1);
  expect(view.result.current.writeUnconfirmed).toBe(true);
  expect(view.result.current.data).toBeNull();
  expect(view.result.current.isPending).toBe(false);
});

it("does not install an old password-write outcome into a new actor context", async () => {
  const reply = deferred<void>();
  const write = vi
    .spyOn(canonicalOrganizationApi, "setMemberPassword")
    .mockReturnValue(reply.promise);
  const changed: CanonicalIdentity = {
    ...identity,
    organization: { id: "new-org", name: "Other" },
    member: { ...identity.member, id: "new-member" },
  };
  const current = {
    ...member,
    organization_id: "new-org",
    member_id: "new-member",
  };
  const view = renderHook(({ actor }) => useCanonicalOrganization(actor), {
    initialProps: { actor: identity },
  });
  await waitFor(() =>
    expect(view.result.current.data?.members).toEqual([member]),
  );
  let outcome!: Promise<boolean>;
  act(() => {
    outcome = view.result.current.setMemberPassword(
      member.member_id,
      "replacement",
    );
  });
  vi.mocked(canonicalOrganizationApi.members).mockResolvedValue([current]);
  vi.mocked(canonicalOrganizationApi.departments).mockResolvedValue([]);
  setAuthTokens("new-access", "new-refresh", false);
  view.rerender({ actor: changed });
  await waitFor(() =>
    expect(view.result.current.data?.members).toEqual([current]),
  );
  await act(async () => {
    reply.resolve();
    expect(await outcome).toBe(false);
  });
  expect(view.result.current.data?.members).toEqual([current]);
  expect(view.result.current.isPending).toBe(false);
  expect(write).toHaveBeenCalledWith(
    member.member_id,
    "replacement",
    "original-access",
  );
});

it("keeps only the current invitation page after an older read completes late", async () => {
  const old = deferred<{ items: []; total: number }>();
  const read = vi
    .spyOn(canonicalOrganizationApi, "invitations")
    .mockImplementation(async (options) =>
      options.status === "used" ? old.promise : { items: [], total: 7 },
    );
  const view = await loaded();
  let earlier!: Promise<boolean>;
  act(() => {
    earlier = view.result.current.loadInvitations({
      limit: 20,
      offset: 20,
      status: "used",
    });
  });
  await act(async () => {
    expect(
      await view.result.current.loadInvitations({ limit: 20, offset: 0 }),
    ).toBe(true);
  });
  await act(async () => {
    old.resolve({ items: [], total: 99 });
    expect(await earlier).toBe(false);
  });
  expect(view.result.current.invitations?.total).toBe(7);
  expect(read).toHaveBeenCalledTimes(2);
  expect(read.mock.calls[1][0]).toEqual({ limit: 20, offset: 0 });
});
