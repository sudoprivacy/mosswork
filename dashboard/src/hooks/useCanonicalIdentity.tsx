import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  canonicalIdentityApi,
  type CanonicalIdentity,
  type CanonicalIdentityStatus,
  type CanonicalMembership,
  type CanonicalTokens,
} from "../api/modules/canonicalIdentity";
import {
  AUTH_TOKEN_CHANGED_EVENT,
  UNAUTHORIZED_EVENT,
  clearAuthToken,
  getAuthRefreshToken,
  getAuthToken,
  isSessionOnlyAuth,
  setAuthTokens,
} from "../api/request";

export function useCanonicalAuthMode() {
  const [status, setStatus] = useState<CanonicalIdentityStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    setStatus(null);
    setError(null);
    void canonicalIdentityApi
      .status()
      .then((value) => {
        if (active) setStatus(value);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason);
      });
    return () => {
      active = false;
    };
  }, [revision]);
  return { status, error, retry: () => setRevision((value) => value + 1) };
}

interface CanonicalIdentityContextValue {
  identity: CanonicalIdentity | null;
  memberships: readonly CanonicalMembership[];
  hasAccessToken: boolean;
  hasRefreshToken: boolean;
  isLoading: boolean;
  isPending: boolean;
  error: unknown;
  logoutUnconfirmed: boolean;
  canSwitchOrganization: boolean;
  reload: () => void;
  login: (phone: string, code: string, remember: boolean) => Promise<boolean>;
  loginWithPassword: (
    username: string,
    password: string,
    remember: boolean,
  ) => Promise<boolean>;
  refresh: () => Promise<boolean>;
  switchOrganization: (organizationId: string) => Promise<boolean>;
  logout: () => Promise<void>;
}

const Context = createContext<CanonicalIdentityContextValue | null>(null);

/** Independent canonical profile; never a MossworkUser or a resource grant. */
export function CanonicalIdentityProvider({
  children,
  passwordLogin = canonicalIdentityApi.mossPasswordLogin,
}: {
  children: ReactNode;
  passwordLogin?: typeof canonicalIdentityApi.passwordLogin;
}) {
  const [token, setToken] = useState(getAuthToken);
  const [identity, setIdentity] = useState<CanonicalIdentity | null>(null);
  const [memberships, setMemberships] = useState<
    readonly CanonicalMembership[]
  >([]);
  const [isLoading, setIsLoading] = useState(Boolean(token));
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [logoutUnconfirmed, setLogoutUnconfirmed] = useState(false);
  const [revision, setRevision] = useState(0);
  // This is a UI request generation, not a login/native/session identifier.
  const generation = useRef(0);
  const credentialGeneration = useRef(0);
  const invalidateProfile = useCallback(() => {
    generation.current++;
  }, []);
  const invalidateCredentials = useCallback(() => {
    invalidateProfile();
    credentialGeneration.current++;
  }, [invalidateProfile]);

  useEffect(() => {
    const changed = () => {
      invalidateCredentials();
      setIdentity(null);
      setMemberships([]);
      setError(null);
      setLogoutUnconfirmed(false);
      setIsPending(false);
      setToken(getAuthToken());
      setIsLoading(Boolean(getAuthToken()));
      setRevision((value) => value + 1);
    };
    const storageChanged = (event: StorageEvent) => {
      if (
        event.key === "auth_token" ||
        event.key === "auth_refresh_pair" ||
        event.key === null
      )
        changed();
    };
    window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, changed);
    window.addEventListener(UNAUTHORIZED_EVENT, changed);
    window.addEventListener("storage", storageChanged);
    return () => {
      invalidateCredentials();
      window.removeEventListener(AUTH_TOKEN_CHANGED_EVENT, changed);
      window.removeEventListener(UNAUTHORIZED_EVENT, changed);
      window.removeEventListener("storage", storageChanged);
    };
  }, [invalidateCredentials]);

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    const fresh = () =>
      current === generation.current && token === getAuthToken();
    if (!token) {
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    void (async () => {
      try {
        const me = await canonicalIdentityApi.me(token, controller.signal);
        if (!fresh()) return;
        setIdentity(me);
        if (me.can_switch_organization !== true) {
          setMemberships([
            { organization: me.organization, member: me.member },
          ]);
          return;
        }
        const list = await canonicalIdentityApi.organizations(
          token,
          controller.signal,
        );
        if (fresh()) setMemberships(list.organizations);
      } catch (reason) {
        if (fresh()) setError(reason);
      } finally {
        if (fresh()) setIsLoading(false);
      }
    })();
    return () => {
      controller.abort();
      invalidateProfile();
    };
  }, [token, revision, invalidateProfile]);

  const reload = useCallback(() => {
    generation.current++;
    setIdentity(null);
    setMemberships([]);
    setError(null);
    setIsLoading(Boolean(getAuthToken()));
    setRevision((value) => value + 1);
  }, []);

  const replaceTokens = async (
    operation: () => Promise<CanonicalTokens>,
    remember: boolean,
    clearRejectedRefresh = false,
  ) => {
    credentialGeneration.current++;
    const current = ++generation.current;
    const access = getAuthToken(),
      refresh = getAuthRefreshToken();
    const fresh = () =>
      current === generation.current &&
      access === getAuthToken() &&
      refresh === getAuthRefreshToken();
    setIsPending(true);
    setError(null);
    setLogoutUnconfirmed(false);
    setIdentity(null);
    setMemberships([]);
    try {
      const result = await operation();
      if (!fresh()) return false;
      if (
        !result.access_token ||
        !result.refresh_token ||
        result.token_type.toLowerCase() !== "bearer"
      )
        throw new Error("Invalid token response");
      setAuthTokens(result.access_token, result.refresh_token, remember);
      return true;
    } catch (reason) {
      if (fresh()) {
        if (
          clearRejectedRefresh &&
          reason instanceof Error &&
          /^Request failed: 401\b/.test(reason.message)
        )
          clearAuthToken();
        setError(reason);
        setIsLoading(false);
      }
      return false;
    } finally {
      if (fresh()) setIsPending(false);
    }
  };

  const logout = async () => {
    const access = getAuthToken(),
      refresh = getAuthRefreshToken();
    // Publish the existing cleanup event immediately. A late server response
    // must never clear a later login or organization selection.
    clearAuthToken();
    const current = credentialGeneration.current;
    try {
      if (access)
        await canonicalIdentityApi.logout(access, refresh || undefined);
    } catch (reason) {
      if (current === credentialGeneration.current && !getAuthToken()) {
        setError(reason);
        setLogoutUnconfirmed(true);
      }
    }
  };

  return (
    <Context.Provider
      value={{
        identity,
        memberships,
        hasAccessToken: Boolean(token),
        hasRefreshToken: Boolean(getAuthRefreshToken()),
        isLoading,
        isPending,
        error,
        logoutUnconfirmed,
        canSwitchOrganization: identity?.can_switch_organization === true,
        reload,
        logout,
        login: (phone, code, remember) =>
          replaceTokens(
            () => canonicalIdentityApi.login(phone, code),
            remember,
          ),
        loginWithPassword: (username, password, remember) =>
          replaceTokens(() => passwordLogin({ username, password }), remember),
        refresh: () => {
          const refresh = getAuthRefreshToken();
          return refresh
            ? replaceTokens(
                () => canonicalIdentityApi.refresh(refresh),
                !isSessionOnlyAuth(),
                true,
              )
            : Promise.resolve(false);
        },
        switchOrganization: (organizationId) => {
          if (identity?.can_switch_organization !== true)
            return Promise.resolve(false);
          const access = getAuthToken();
          return replaceTokens(
            () =>
              canonicalIdentityApi.switchOrganization(organizationId, access),
            !isSessionOnlyAuth(),
          );
        },
      }}
    >
      {children}
    </Context.Provider>
  );
}

export function useCanonicalIdentity(): CanonicalIdentityContextValue {
  const context = useContext(Context);
  if (!context) throw new Error("CanonicalIdentityProvider required");
  return context;
}
