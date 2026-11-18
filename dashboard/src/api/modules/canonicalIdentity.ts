import { clearSetupRequired, request } from "../request";

export interface CanonicalIdentityStatus {
  mode: "standalone" | "canonical";
  phone_login_available: boolean;
  password_login_available: boolean;
}

export interface CanonicalOrganization {
  readonly id: string;
  readonly name: string;
}

export interface CanonicalMember {
  readonly id: string;
  readonly role: string;
  readonly status: string;
  readonly revision: number;
}

/** Verified server profile; role and entitlement are not resource permission proofs. */
export interface CanonicalIdentity {
  readonly account_id: string;
  readonly organization: CanonicalOrganization;
  readonly member: CanonicalMember;
  readonly moss_access: {
    readonly is_enabled: boolean;
    readonly is_granted: boolean;
    readonly source: "role" | "explicit";
  };
  readonly personal_agent_ref: string | null;
  readonly can_switch_organization?: boolean;
}

export interface CanonicalMembership {
  readonly organization: CanonicalOrganization;
  readonly member: CanonicalMember;
}

export interface CanonicalTokens {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: string;
  account_id: string;
  organization_id: string;
  member_id: string;
}

export interface CanonicalPasswordInput {
  username?: string;
  email?: string;
  password: string;
}

export interface CanonicalProfile {
  readonly account_id: string;
  readonly organization_id: string;
  readonly member_id: string;
  readonly login_name: string;
  readonly nickname: string;
  readonly avatar: string | null;
  readonly can_change_password: boolean;
}

const bearer = (token: string): HeadersInit => ({
  Authorization: `Bearer ${token}`,
});
const post = <T>(path: string, body: object, token = "") =>
  request<T>(`/identity/${path}`, {
    method: "POST",
    body: JSON.stringify(body),
    headers: bearer(token),
  });

const saveProfile = async (path: string, body: object, token: string) => {
  const result = await post<unknown>(path, body, token);
  if (
    !result ||
    typeof result !== "object" ||
    !("success" in result) ||
    result.success !== true
  )
    throw new Error("canonical.profileSaveUnconfirmed");
  return { success: true as const };
};

export const canonicalIdentityApi = {
  status: async (): Promise<CanonicalIdentityStatus> => {
    // The trusted mode probe must precede the standalone setup latch.
    clearSetupRequired();
    const status = await request<CanonicalIdentityStatus>("/identity/status");
    if (
      (status.mode !== "canonical" && status.mode !== "standalone") ||
      typeof status.phone_login_available !== "boolean" ||
      typeof status.password_login_available !== "boolean"
    )
      throw new Error("Invalid identity mode");
    return status;
  },
  me: (token: string, signal?: AbortSignal) =>
    request<CanonicalIdentity>("/identity/me", {
      headers: bearer(token),
      signal,
    }),
  profile: (token: string, signal?: AbortSignal) =>
    request<CanonicalProfile>("/identity/profile", {
      headers: bearer(token),
      signal,
    }),
  updateNickname: (nickname: string, token: string) =>
    saveProfile("profile/nickname", { nickname }, token),
  updateAvatar: async (avatar: string | null, token: string) => {
    const result = await post<unknown>("profile/avatar", { avatar }, token);
    if (!result || typeof result !== "object" || !("avatar" in result))
      throw new Error("canonical.profileSaveUnconfirmed");
    const normalized = result.avatar;
    if (normalized === null) {
      if (avatar !== null) throw new Error("canonical.profileSaveUnconfirmed");
      return { avatar: null };
    }
    if (
      avatar === null ||
      typeof normalized !== "string" ||
      !normalized.startsWith("data:image/webp;base64,")
    )
      throw new Error("canonical.profileSaveUnconfirmed");
    return { avatar: normalized };
  },
  changePassword: (oldPassword: string, newPassword: string, token: string) =>
    saveProfile("password/change", { oldPassword, newPassword }, token),
  organizations: (token: string, signal?: AbortSignal) =>
    request<{ organizations: CanonicalMembership[] }>(
      "/identity/organizations",
      { headers: bearer(token), signal },
    ),
  sendCode: (phone: string) =>
    post<{ next_send_in: number; delivery: "tencent" }>("phone/send-code", {
      phone,
    }),
  login: (phone: string, code: string) =>
    post<CanonicalTokens>("phone/login", { phone, code }),
  passwordLogin: (input: CanonicalPasswordInput) =>
    post<CanonicalTokens>("password/login", input),
  mossPasswordLogin: (input: CanonicalPasswordInput) =>
    post<CanonicalTokens>("moss/password/login", input),
  refresh: (refreshToken: string) =>
    post<CanonicalTokens>("refresh", { refresh_token: refreshToken }),
  switchOrganization: (organizationId: string, token: string) =>
    post<CanonicalTokens>(
      "switch-organization",
      { organization_id: organizationId },
      token,
    ),
  logout: (token: string, refreshToken?: string) =>
    post<void>(
      "logout",
      refreshToken ? { refresh_token: refreshToken } : {},
      token,
    ),
};
