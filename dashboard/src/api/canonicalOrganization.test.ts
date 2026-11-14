import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { canonicalOrganizationApi } from "./modules/canonicalOrganization";
import { clearSetupRequired } from "./request";

vi.mock("./config", () => ({ getApiUrl: (path: string) => `/api${path}` }));
vi.mock("../i18n", () => ({ default: { language: "en" } }));
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  clearSetupRequired();
});
afterEach(() => vi.unstubAllGlobals());

const member = {
  account_id: null,
  organization_id: "original-org",
  member_id: "original-member",
  username: "retained-user",
  display_name: null,
  role: "user",
  status: "disabled",
  membership_revision: 1,
  created_at: 1234,
  legacy_id: -42,
  department_id: null,
};
const department = {
  department_id: "original-department",
  organization_id: "original-org",
  parent_id: null,
  name: "Original department",
  created_at: 1234,
  updated_at: 1235,
  user_count: 0,
  external_id: "original-external",
};
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
function transport(value: unknown, status = 200) {
  const calls: { url: string; options: RequestInit | undefined }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      calls.push({ url, options });
      return json(value, status);
    }),
  );
  return calls;
}

it("reads original member facts including disabled and unassigned rows without integer projection", async () => {
  const calls = transport({ members: [member] });
  expect(
    await canonicalOrganizationApi.members("held-original-access"),
  ).toEqual([member]);
  expect(calls[0].url).toBe("/api/identity/members");
  expect(new Headers(calls[0].options?.headers).get("Authorization")).toBe(
    "Bearer held-original-access",
  );
  expect(calls[0].options?.body).toBeUndefined();
});

it("refuses malformed directory counts without dropping individual source rows", async () => {
  const calls = transport({
    departments: [{ ...department, user_count: "0" }],
  });
  await expect(
    canonicalOrganizationApi.departments("held-original-access"),
  ).rejects.toThrow("canonicalOrganization.readUnavailable");
  expect(calls).toHaveLength(1);
});

it("creates through the same department collection with no organization or grant in the body", async () => {
  const calls = transport({ department });
  expect(
    await canonicalOrganizationApi.createDepartment(
      {
        name: "Current name",
        parent_id: "original-parent",
        external_id: "directory-id",
      },
      "held-original-access",
    ),
  ).toEqual(department);
  expect(calls[0].url).toBe("/api/identity/departments");
  expect(calls[0].options?.method).toBe("POST");
  expect(JSON.parse(String(calls[0].options?.body))).toEqual({
    name: "Current name",
    parent_id: "original-parent",
    external_id: "directory-id",
  });
});

it("distinguishes omitted update fields from explicit null without retargeting the department", async () => {
  const calls = transport({ department });
  await canonicalOrganizationApi.updateDepartment(
    "original-department",
    { name: "Changed" },
    "held-original-access",
  );
  await canonicalOrganizationApi.updateDepartment(
    "original-department",
    { parent_id: null, external_id: null },
    "held-original-access",
  );
  expect(calls.map((call) => call.url)).toEqual([
    "/api/identity/departments/original-department",
    "/api/identity/departments/original-department",
  ]);
  expect(JSON.parse(String(calls[0].options?.body))).toEqual({
    name: "Changed",
  });
  expect(JSON.parse(String(calls[1].options?.body))).toEqual({
    parent_id: null,
    external_id: null,
  });
  expect(calls.every((call) => call.options?.method === "PATCH")).toBe(true);
});

it("withholds a malformed non-error write reply after exactly one possible effect", async () => {
  let effects = 0;
  const fetchMock = vi.fn(async () => {
    effects++;
    return json({ department: { ...department, external_id: 42 } });
  });
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    canonicalOrganizationApi.createDepartment(
      { name: "Committed", parent_id: null },
      "held-original-access",
    ),
  ).rejects.toThrow("canonicalOrganization.writeUnconfirmed");
  expect(effects).toBe(1);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("uses DELETE only for the original department and sends no synthetic cleanup body", async () => {
  const calls = transport({ ok: true });
  await canonicalOrganizationApi.deleteDepartment(
    "original-department",
    "held-original-access",
  );
  expect(calls[0].url).toBe("/api/identity/departments/original-department");
  expect(calls[0].options?.method).toBe("DELETE");
  expect(calls[0].options?.body).toBeUndefined();
});

it("keeps the original member identifier and exact admin password semantics", async () => {
  const calls = transport({ ok: true });
  await canonicalOrganizationApi.setMemberPassword(
    "original-member",
    "  explicit password  ",
    "held-original-access",
  );
  expect(calls[0].url).toBe("/api/identity/members/original-member/password");
  expect(JSON.parse(String(calls[0].options?.body))).toEqual({
    password: "  explicit password  ",
  });
  expect(calls).toHaveLength(1);
});

it("does not retry an original member write after an HTTP refusal", async () => {
  const calls = transport(
    { error: { code: "INTERNAL_ERROR", message: "Unavailable" } },
    503,
  );
  await expect(
    canonicalOrganizationApi.setMemberPassword(
      "original-member",
      "replacement",
      "held-original-access",
    ),
  ).rejects.toThrow(/503/);
  expect(calls).toHaveLength(1);
});

it("adds an existing registered account through its original phone-only port", async () => {
  const calls = transport({ member, created: false });
  expect(
    await canonicalOrganizationApi.addRegisteredMember(
      "+8613800000000",
      "held-original-access",
    ),
  ).toEqual({ member, created: false });
  expect(calls[0].url).toBe("/api/identity/members");
  expect(JSON.parse(String(calls[0].options?.body))).toEqual({
    phone: "+8613800000000",
  });
  expect(calls).toHaveLength(1);
});

it("retains invitation pagination and removes a cleared status filter", async () => {
  const calls = transport({ items: [], total: 0 });
  await canonicalOrganizationApi.invitations(
    { limit: 20, offset: 20, status: "used" },
    "held-original-access",
  );
  await canonicalOrganizationApi.invitations(
    { limit: 20, offset: 0 },
    "held-original-access",
  );
  expect(calls.map((call) => call.url)).toEqual([
    "/api/identity/invitations?limit=20&offset=20&status=used",
    "/api/identity/invitations?limit=20&offset=0",
  ]);
  expect(calls.every((call) => call.options?.body === undefined)).toBe(true);
});
