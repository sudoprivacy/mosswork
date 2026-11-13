import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import {
  canonicalIdentityApi,
  type CanonicalIdentity,
  type CanonicalTokens,
} from "../api/modules/canonicalIdentity";
import {
  getAuthRefreshToken,
  getAuthToken,
  setAuthTokens,
} from "../api/request";
import {
  CanonicalIdentityProvider,
  useCanonicalIdentity,
} from "./useCanonicalIdentity";
import { useCurrentUser } from "./useCurrentUser";

vi.mock("../api/modules/canonicalIdentity", () => ({
  canonicalIdentityApi: {
    me: vi.fn(),
    organizations: vi.fn(),
    refresh: vi.fn(),
    switchOrganization: vi.fn(),
    logout: vi.fn(),
    login: vi.fn(),
    mossPasswordLogin: vi.fn(),
  },
}));
vi.mock("../i18n", () => ({ default: { language: "en" } }));

const profile = (company: string): CanonicalIdentity => ({
  account_id: "human-uuid",
  organization: { id: company, name: company },
  member: {
    id: `${company}-member-uuid`,
    role: "admin",
    status: "active",
    revision: 1,
  },
  moss_access: { is_enabled: true, is_granted: true, source: "role" },
  personal_agent_ref: "moss://personal",
  can_switch_organization: true,
});
const tokens = (value: string): CanonicalTokens => ({
  access_token: value,
  refresh_token: `${value}-refresh`,
  token_type: "Bearer",
  expires_in: 300,
  account_id: "human-uuid",
  organization_id: "company-uuid",
  member_id: "member-uuid",
});
const deferred = <T,>() => {
  let resolve!: (value: T) => void, reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
function Probe() {
  const session = useCanonicalIdentity();
  const local = useCurrentUser();
  return (
    <>
      <div>{session.identity?.organization.name ?? "empty"}</div>
      <div>local:{local?.id ?? "null"}</div>
      <div>
        members:
        {session.memberships.map((item) => item.organization.name).join(",")}
      </div>
      {session.error ? <div>operation-error</div> : null}
      <button onClick={() => void session.refresh()}>refresh</button>
      <button onClick={() => void session.logout()}>logout</button>
      <button
        onClick={() =>
          void session.loginWithPassword("home", "public-password", false)
        }
      >
        password-login
      </button>
      <button onClick={() => void session.login("13900000001", "123456", true)}>
        sms-login
      </button>
      <button onClick={() => void session.switchOrganization("next")}>
        switch
      </button>
      <div>can-switch:{String(session.canSwitchOrganization)}</div>
    </>
  );
}
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.resetAllMocks();
  vi.mocked(canonicalIdentityApi.me).mockImplementation(async (token) =>
    profile(token),
  );
  vi.mocked(canonicalIdentityApi.organizations).mockResolvedValue({
    organizations: [],
  });
  vi.mocked(canonicalIdentityApi.logout).mockResolvedValue(undefined);
  setAuthTokens("old", "old-refresh");
});
const mount = () =>
  render(
    <CanonicalIdentityProvider>
      <Probe />
    </CanonicalIdentityProvider>,
  );

it("keeps canonical admin separate from the integer local profile", async () => {
  mount();
  await screen.findByText("old");
  expect(screen.getByText("local:null")).toBeInTheDocument();
});
it("discards a late old me after a company/token change", async () => {
  const old = deferred<CanonicalIdentity>();
  vi.mocked(canonicalIdentityApi.me).mockImplementationOnce(() => old.promise);
  mount();
  await waitFor(() => expect(canonicalIdentityApi.me).toHaveBeenCalledOnce());
  act(() => setAuthTokens("new", "new-refresh"));
  await screen.findByText("new");
  await act(async () => old.resolve(profile("stale")));
  expect(screen.queryByText("stale")).toBeNull();
  expect(getAuthToken()).toBe("new");
});
it.each(["refresh", "switch"] as const)(
  "discards late %s success after another login",
  async (action) => {
    const old = deferred<CanonicalTokens>();
    vi.mocked(
      action === "refresh"
        ? canonicalIdentityApi.refresh
        : canonicalIdentityApi.switchOrganization,
    ).mockReturnValue(old.promise);
    mount();
    await screen.findByText("old");
    fireEvent.click(screen.getByText(action));
    act(() => setAuthTokens("new", "new-refresh"));
    await screen.findByText("new");
    await act(async () => old.resolve(tokens("stale")));
    expect(getAuthToken()).toBe("new");
    expect(getAuthRefreshToken()).toBe("new-refresh");
    expect(screen.queryByText("stale")).toBeNull();
  },
);
it("never clears a new login after an old refresh 401", async () => {
  const old = deferred<CanonicalTokens>();
  vi.mocked(canonicalIdentityApi.refresh).mockReturnValue(old.promise);
  mount();
  await screen.findByText("old");
  fireEvent.click(screen.getByText("refresh"));
  act(() => setAuthTokens("new", "new-refresh"));
  await screen.findByText("new");
  await act(async () =>
    old.reject(new Error("Request failed: 401 Unauthorized")),
  );
  expect(getAuthToken()).toBe("new");
  expect(getAuthRefreshToken()).toBe("new-refresh");
  expect(screen.queryByText("operation-error")).toBeNull();
});

it("rejects a late refresh after the refresh pair changes with the same access token", async () => {
  const old = deferred<CanonicalTokens>();
  vi.mocked(canonicalIdentityApi.refresh).mockReturnValue(old.promise);
  mount();
  await screen.findByText("old");
  fireEvent.click(screen.getByText("refresh"));
  act(() => setAuthTokens("old", "rotated-refresh"));
  await screen.findByText("old");
  await act(async () => old.resolve(tokens("stale")));
  expect(getAuthToken()).toBe("old");
  expect(getAuthRefreshToken()).toBe("rotated-refresh");
});
it("clears a rejected refresh only when its captured pair is still current", async () => {
  vi.mocked(canonicalIdentityApi.refresh).mockRejectedValue(
    new Error("Request failed: 401 Unauthorized"),
  );
  mount();
  await screen.findByText("old");
  fireEvent.click(screen.getByText("refresh"));
  await waitFor(() => expect(getAuthToken()).toBe(""));
  expect(getAuthRefreshToken()).toBe("");
});
it("logout clears immediately and its late failure cannot affect a new login", async () => {
  const old = deferred<void>();
  vi.mocked(canonicalIdentityApi.logout).mockReturnValue(old.promise);
  mount();
  await screen.findByText("old");
  fireEvent.click(screen.getByText("logout"));
  expect(getAuthToken()).toBe("");
  act(() => setAuthTokens("new", "new-refresh"));
  await screen.findByText("new");
  await act(async () => old.reject(new TypeError("Failed to fetch")));
  expect(getAuthToken()).toBe("new");
  expect(screen.queryByText("operation-error")).toBeNull();
});
it("does not invent membership access when organizations is forbidden", async () => {
  vi.mocked(canonicalIdentityApi.organizations).mockRejectedValue(
    new Error("Forbidden"),
  );
  mount();
  await screen.findByText("old");
  await screen.findByText("operation-error");
  expect(getAuthToken()).toBe("old");
  expect(screen.getByText("local:null")).toBeInTheDocument();
});

it.each([false, undefined])(
  "uses the verified current profile without requesting other organizations, capability=%s",
  async (capability) => {
    vi.mocked(canonicalIdentityApi.me).mockResolvedValue({
      ...profile("current"),
      can_switch_organization: capability,
    });
    vi.mocked(canonicalIdentityApi.organizations).mockRejectedValue(
      new Error("Forbidden"),
    );
    mount();
    await screen.findByText("current");
    expect(screen.getByText("members:current")).toBeInTheDocument();
    expect(screen.getByText("can-switch:false")).toBeInTheDocument();
    expect(canonicalIdentityApi.organizations).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("switch"));
    expect(canonicalIdentityApi.switchOrganization).not.toHaveBeenCalled();
    expect(screen.queryByText("operation-error")).toBeNull();
    expect(getAuthToken()).toBe("old");
  },
);

it("uses the dedicated Moss password API and the central remembered pair", async () => {
  vi.mocked(canonicalIdentityApi.mossPasswordLogin).mockResolvedValue(
    tokens("password"),
  );
  mount();
  await screen.findByText("old");
  fireEvent.click(screen.getByText("password-login"));
  await screen.findByText("password");
  expect(canonicalIdentityApi.mossPasswordLogin).toHaveBeenCalledWith({
    username: "home",
    password: "public-password",
  });
  expect(sessionStorage.getItem("auth_token")).toBe("password");
  expect(localStorage.getItem("auth_token")).toBeNull();
  expect(getAuthRefreshToken()).toBe("password-refresh");
});

it.each(["sms", "company", "logout"] as const)(
  "discards a late password success after %s",
  async (action) => {
    const old = deferred<CanonicalTokens>();
    vi.mocked(canonicalIdentityApi.mossPasswordLogin).mockReturnValue(
      old.promise,
    );
    vi.mocked(canonicalIdentityApi.login).mockResolvedValue(tokens("sms"));
    mount();
    await screen.findByText("old");
    fireEvent.click(screen.getByText("password-login"));
    if (action === "sms") {
      fireEvent.click(screen.getByText("sms-login"));
      await screen.findByText("sms");
    } else if (action === "company") {
      act(() => setAuthTokens("company", "company-refresh"));
      await screen.findByText("company");
    } else {
      fireEvent.click(screen.getByText("logout"));
      await waitFor(() => expect(getAuthToken()).toBe(""));
    }
    await act(async () => old.resolve(tokens("stale-password")));
    expect(getAuthToken()).toBe(action === "logout" ? "" : action);
    expect(screen.queryByText("stale-password")).toBeNull();
  },
);

it("a late password failure cannot clear a newer SMS login or show its error", async () => {
  const old = deferred<CanonicalTokens>();
  vi.mocked(canonicalIdentityApi.mossPasswordLogin).mockReturnValue(
    old.promise,
  );
  vi.mocked(canonicalIdentityApi.login).mockResolvedValue(tokens("sms"));
  mount();
  await screen.findByText("old");
  fireEvent.click(screen.getByText("password-login"));
  fireEvent.click(screen.getByText("sms-login"));
  await screen.findByText("sms");
  await act(async () =>
    old.reject(new Error("Request failed: 401 Unauthorized")),
  );
  expect(getAuthToken()).toBe("sms");
  expect(getAuthRefreshToken()).toBe("sms-refresh");
  expect(screen.queryByText("operation-error")).toBeNull();
});

it("discards the old phone membership list after a password login without switching capability", async () => {
  const oldList = deferred<{
    organizations: {
      organization: CanonicalIdentity["organization"];
      member: CanonicalIdentity["member"];
    }[];
  }>();
  vi.mocked(canonicalIdentityApi.organizations).mockReturnValueOnce(
    oldList.promise,
  );
  vi.mocked(canonicalIdentityApi.me).mockImplementation(async (token) => ({
    ...profile(token),
    can_switch_organization: token !== "password",
  }));
  vi.mocked(canonicalIdentityApi.mossPasswordLogin).mockResolvedValue(
    tokens("password"),
  );
  mount();
  await screen.findByText("old");
  fireEvent.click(screen.getByText("password-login"));
  await screen.findByText("password");
  await act(async () =>
    oldList.resolve({
      organizations: [
        {
          organization: { id: "stale-org", name: "stale-company" },
          member: profile("stale").member,
        },
      ],
    }),
  );
  expect(screen.getByText("members:password")).toBeInTheDocument();
  expect(screen.getByText("can-switch:false")).toBeInTheDocument();
  expect(canonicalIdentityApi.organizations).toHaveBeenCalledOnce();
  expect(screen.queryByText("members:stale-company")).toBeNull();
});
