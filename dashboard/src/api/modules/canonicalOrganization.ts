import { request } from "../request";

export interface CanonicalOrganizationMember {
  readonly account_id: string | null;
  readonly organization_id: string;
  readonly member_id: string;
  readonly username: string;
  readonly display_name: string | null;
  readonly role: string;
  readonly status: string;
  readonly membership_revision: number;
  readonly created_at: number;
  readonly legacy_id: number | null;
  readonly department_id: string | null;
}

export interface CanonicalDepartment {
  readonly department_id: string;
  readonly organization_id: string;
  readonly parent_id: string | null;
  readonly name: string;
  readonly created_at: number;
  readonly updated_at: number;
  readonly user_count: number;
}

export interface CanonicalDepartmentDetails extends CanonicalDepartment {
  readonly external_id: string | null;
}

export interface CanonicalInvitation {
  readonly invitation_id: string;
  readonly organization_id: string;
  readonly code: string;
  readonly status: "pending" | "used" | "revoked";
  readonly initial_credit_units: number;
  readonly legacy_initial_quota_usd: number | null;
  readonly used_by_member_id: string | null;
  readonly created_at: number;
  readonly used_at: number | null;
}

export interface DepartmentChanges {
  readonly name?: string;
  readonly parent_id?: string | null;
  readonly external_id?: string | null;
}

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const textOrNull = (value: unknown): value is string | null =>
  value === null || typeof value === "string";
const count = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const integer = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value);
const numberOrNull = (value: unknown): value is number | null =>
  value === null || (typeof value === "number" && Number.isFinite(value));

function member(value: unknown): value is CanonicalOrganizationMember {
  return (
    record(value) &&
    textOrNull(value.account_id) &&
    typeof value.organization_id === "string" &&
    !!value.organization_id &&
    typeof value.member_id === "string" &&
    !!value.member_id &&
    typeof value.username === "string" &&
    textOrNull(value.display_name) &&
    typeof value.role === "string" &&
    typeof value.status === "string" &&
    integer(value.membership_revision) &&
    integer(value.created_at) &&
    (value.legacy_id === null || integer(value.legacy_id)) &&
    textOrNull(value.department_id)
  );
}

function department(value: unknown): value is CanonicalDepartment {
  return (
    record(value) &&
    typeof value.department_id === "string" &&
    !!value.department_id &&
    typeof value.organization_id === "string" &&
    !!value.organization_id &&
    textOrNull(value.parent_id) &&
    typeof value.name === "string" &&
    integer(value.created_at) &&
    integer(value.updated_at) &&
    count(value.user_count)
  );
}

function invitation(value: unknown): value is CanonicalInvitation {
  return (
    record(value) &&
    typeof value.invitation_id === "string" &&
    !!value.invitation_id &&
    typeof value.organization_id === "string" &&
    !!value.organization_id &&
    typeof value.code === "string" &&
    (value.status === "pending" ||
      value.status === "used" ||
      value.status === "revoked") &&
    typeof value.initial_credit_units === "number" &&
    Number.isFinite(value.initial_credit_units) &&
    numberOrNull(value.legacy_initial_quota_usd) &&
    textOrNull(value.used_by_member_id) &&
    integer(value.created_at) &&
    (value.used_at === null || integer(value.used_at))
  );
}

function rows<T>(
  value: unknown,
  key: string,
  valid: (row: unknown) => row is T,
): T[] {
  if (!record(value) || !Array.isArray(value[key]) || !value[key].every(valid))
    throw new Error("canonicalOrganization.readUnavailable");
  return value[key];
}

const headers = (token: string) => ({ Authorization: `Bearer ${token}` });
const read = (path: string, token: string, signal?: AbortSignal) =>
  request<unknown>(`/identity/${path}`, { headers: headers(token), signal });
const write = (
  path: string,
  method: "POST" | "PATCH" | "DELETE",
  body: object | undefined,
  token: string,
) =>
  request<unknown>(`/identity/${path}`, {
    method,
    headers: headers(token),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });

function writtenDepartment(value: unknown): CanonicalDepartmentDetails {
  if (
    !record(value) ||
    !department(value.department) ||
    !record(value.department) ||
    !textOrNull(value.department.external_id)
  )
    throw new Error("canonicalOrganization.writeUnconfirmed");
  return value.department as unknown as CanonicalDepartmentDetails;
}

function confirmed(value: unknown): void {
  if (!record(value) || value.ok !== true)
    throw new Error("canonicalOrganization.writeUnconfirmed");
}

/** Existing business HTTP ports only; these responses confer no runtime grants. */
export const canonicalOrganizationApi = {
  members: async (token: string, signal?: AbortSignal) =>
    rows(await read("members", token, signal), "members", member),
  departments: async (token: string, signal?: AbortSignal) =>
    rows(await read("departments", token, signal), "departments", department),
  invitations: async (
    options: {
      readonly status?: CanonicalInvitation["status"];
      readonly offset: number;
      readonly limit: number;
    },
    token: string,
    signal?: AbortSignal,
  ) => {
    const query = new URLSearchParams({
      limit: String(options.limit),
      offset: String(options.offset),
    });
    if (options.status) query.set("status", options.status);
    const value = await read(`invitations?${query}`, token, signal);
    const items = rows(value, "items", invitation);
    if (!record(value) || !count(value.total))
      throw new Error("canonicalOrganization.readUnavailable");
    return { items, total: value.total };
  },
  createDepartment: async (
    input: {
      readonly name: string;
      readonly parent_id: string | null;
      readonly external_id?: string | null;
    },
    token: string,
  ) => writtenDepartment(await write("departments", "POST", input, token)),
  updateDepartment: async (
    originalId: string,
    changes: DepartmentChanges,
    token: string,
  ) =>
    writtenDepartment(
      await write(
        `departments/${encodeURIComponent(originalId)}`,
        "PATCH",
        changes,
        token,
      ),
    ),
  deleteDepartment: async (originalId: string, token: string) =>
    confirmed(
      await write(
        `departments/${encodeURIComponent(originalId)}`,
        "DELETE",
        undefined,
        token,
      ),
    ),
  setMemberPassword: async (
    originalMemberId: string,
    password: string,
    token: string,
  ) =>
    confirmed(
      await write(
        `members/${encodeURIComponent(originalMemberId)}/password`,
        "POST",
        { password },
        token,
      ),
    ),
  addRegisteredMember: async (phone: string, token: string) => {
    const value = await write("members", "POST", { phone }, token);
    if (
      !record(value) ||
      !member(value.member) ||
      typeof value.created !== "boolean"
    )
      throw new Error("canonicalOrganization.writeUnconfirmed");
    return { member: value.member, created: value.created };
  },
};
