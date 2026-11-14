import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Select,
  Segmented,
  Space,
  Spin,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import { canonicalIdentityApi } from "../../api/modules/canonicalIdentity";
import {
  CanonicalIdentityProvider,
  useCanonicalIdentity,
} from "../../hooks/useCanonicalIdentity";
import { parseApiError } from "../../utils/apiError";
import { productLogo } from "../../utils/branding";
import { CanonicalProfilePanel } from "./CanonicalProfilePanel";
import { CanonicalOrganizationPanel } from "./CanonicalOrganizationPanel";
import { showsOrganizationDirectory } from "../../hooks/useCanonicalOrganization";

function CanonicalError({
  error,
  logoutUnconfirmed = false,
}: {
  error: unknown;
  logoutUnconfirmed?: boolean;
}) {
  const { t } = useTranslation();
  if (!error) return null;
  const code = parseApiError(error)?.code;
  const key = code ? `apiErrors.${code}` : "canonical.error";
  const text = logoutUnconfirmed
    ? t("canonical.logoutUnconfirmed")
    : t(key) !== key
    ? t(key)
    : t("canonical.error");
  return <Alert type="error" showIcon message={text} />;
}

export function CanonicalPhoneLogin({
  isAvailable,
  isPasswordAvailable = false,
  onAuthenticated,
}: {
  isAvailable: boolean;
  isPasswordAvailable?: boolean;
  onAuthenticated?: () => void;
}) {
  const { t } = useTranslation();
  const session = useCanonicalIdentity();
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [method, setMethod] = useState<"phone" | "password">(
    isAvailable ? "phone" : "password",
  );
  const selectedMethod = !isPasswordAvailable
    ? "phone"
    : !isAvailable
    ? "password"
    : method;
  const isMethodAvailable =
    selectedMethod === "phone" ? isAvailable : isPasswordAvailable;
  const [remember, setRemember] = useState(true);
  const [isSending, setIsSending] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const generation = useRef(0);
  useEffect(
    () => () => {
      generation.current++;
    },
    [],
  );
  useEffect(() => {
    if (seconds <= 0) return;
    const timer = window.setTimeout(
      () => setSeconds((value) => Math.max(0, value - 1)),
      1000,
    );
    return () => window.clearTimeout(timer);
  }, [seconds]);
  const send = async () => {
    const current = ++generation.current;
    setIsSending(true);
    setError(null);
    try {
      const result = await canonicalIdentityApi.sendCode(phone);
      if (current === generation.current)
        setSeconds(Math.max(0, Math.ceil(result.next_send_in)));
    } catch (reason) {
      if (current === generation.current) setError(reason);
    } finally {
      if (current === generation.current) setIsSending(false);
    }
  };
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Typography.Title level={3}>{t("canonical.title")}</Typography.Title>
      {!isAvailable && !isPasswordAvailable && (
        <Alert type="info" message={t("canonical.unavailable")} />
      )}
      <CanonicalError
        error={error || session.error}
        logoutUnconfirmed={session.logoutUnconfirmed}
      />
      {isAvailable && isPasswordAvailable && (
        <Segmented
          block
          value={selectedMethod}
          disabled={session.isPending}
          options={[
            { value: "phone", label: t("canonical.phoneSignIn") },
            { value: "password", label: t("canonical.passwordSignIn") },
          ]}
          onChange={(value) => {
            if (value === "phone" || value === "password") setMethod(value);
            setError(null);
          }}
        />
      )}
      <Form
        layout="vertical"
        onFinish={async () => {
          if (!isMethodAvailable) return;
          const authenticated =
            selectedMethod === "phone"
              ? await session.login(phone, code, remember)
              : await session.loginWithPassword(username, password, remember);
          if (authenticated) {
            setPassword("");
            onAuthenticated?.();
          }
        }}
      >
        {selectedMethod === "phone" ? (
          <>
            <Form.Item label={t("canonical.phone")} htmlFor="canonical-phone">
              <Input
                id="canonical-phone"
                type="tel"
                autoComplete="tel"
                value={phone}
                maxLength={64}
                disabled={!isAvailable || session.isPending}
                onChange={(event) => {
                  generation.current++;
                  setIsSending(false);
                  setPhone(event.target.value);
                  setCode("");
                  setSeconds(0);
                  setError(null);
                }}
              />
            </Form.Item>
          </>
        ) : (
          <>
            <Form.Item
              label={t("canonical.username")}
              htmlFor="canonical-username"
            >
              <Input
                id="canonical-username"
                autoComplete="username"
                value={username}
                disabled={!isPasswordAvailable || session.isPending}
                onChange={(event) => setUsername(event.target.value)}
              />
            </Form.Item>
            <Form.Item
              label={t("canonical.password")}
              htmlFor="canonical-password"
            >
              <Input.Password
                id="canonical-password"
                autoComplete="current-password"
                value={password}
                disabled={!isPasswordAvailable || session.isPending}
                onChange={(event) => setPassword(event.target.value)}
              />
            </Form.Item>
          </>
        )}
        {selectedMethod === "phone" && (
          <Form.Item label={t("canonical.code")} htmlFor="canonical-code">
            <Space.Compact style={{ width: "100%" }}>
              <Input
                id="canonical-code"
                autoComplete="one-time-code"
                inputMode="numeric"
                value={code}
                maxLength={32}
                disabled={!isAvailable || session.isPending}
                onChange={(event) => setCode(event.target.value)}
              />
              <Button
                disabled={
                  !isAvailable ||
                  !phone.trim() ||
                  seconds > 0 ||
                  session.isPending
                }
                loading={isSending}
                onClick={() => void send()}
              >
                {seconds > 0
                  ? t("canonical.sendAgain", { seconds })
                  : t("canonical.sendCode")}
              </Button>
            </Space.Compact>
          </Form.Item>
        )}
        <Form.Item>
          <Checkbox
            checked={remember}
            onChange={(event) => setRemember(event.target.checked)}
          >
            {t("canonical.remember")}
          </Checkbox>
        </Form.Item>
        <Button
          block
          type="primary"
          htmlType="submit"
          loading={session.isPending}
          disabled={
            !isMethodAvailable ||
            (selectedMethod === "phone"
              ? !phone.trim() || !code.trim()
              : !username.trim() || !password)
          }
        >
          {t("canonical.signIn")}
        </Button>
      </Form>
    </Space>
  );
}

/** Safe account landing until the separately owned resource/runtime UI is wired. */
export function CanonicalAccountScreen({
  phoneLoginAvailable,
  passwordLoginAvailable = false,
  onAuthenticated,
  alternateLogin,
}: {
  phoneLoginAvailable: boolean;
  passwordLoginAvailable?: boolean;
  onAuthenticated?: () => void;
  alternateLogin?: { readonly href: string; readonly label: string };
}) {
  const { t } = useTranslation();
  const session = useCanonicalIdentity();
  const [showProfile, setShowProfile] = useState(false);
  const [showOrganization, setShowOrganization] = useState(false);
  useEffect(() => {
    setShowProfile(false);
    setShowOrganization(false);
  }, [
    session.identity?.account_id,
    session.identity?.organization.id,
    session.identity?.member.id,
  ]);
  return (
    <div
      style={{
        minHeight: "100dvh",
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr)",
        placeItems: "center",
        background: "var(--fn-bg-layout)",
        padding: 24,
      }}
    >
      <Card
        style={{
          width: "100%",
          minWidth: 0,
          maxWidth:
            showOrganization &&
            showsOrganizationDirectory(session.identity?.member.role ?? "")
              ? 1080
              : 440,
        }}
      >
        <img
          src={productLogo(false)}
          alt="Mosswork"
          height={36}
          style={{ marginBottom: 24 }}
        />
        {session.isLoading || (session.isPending && session.hasAccessToken) ? (
          <Spin />
        ) : session.identity ? (
          <Space direction="vertical" size="large" style={{ width: "100%" }}>
            <Typography.Title level={3}>
              {t("canonical.account")}
            </Typography.Title>
            <Alert type="info" message={t("canonical.runtimePending")} />
            <CanonicalError error={session.error} />
            <Typography.Text>
              {session.identity.organization.name}
            </Typography.Text>
            <Typography.Text>
              {t("canonical.role")}: {session.identity.member.role}
            </Typography.Text>
            <Select
              aria-label={t("canonical.organization")}
              style={{ width: "100%" }}
              value={session.identity.organization.id}
              disabled={
                !session.canSwitchOrganization ||
                session.isPending ||
                session.memberships.length < 2
              }
              options={session.memberships.map((item) => ({
                value: item.organization.id,
                label: item.organization.name,
              }))}
              onChange={(id: string) => void session.switchOrganization(id)}
            />
            <Space wrap>
              <Button
                disabled={!session.hasRefreshToken || session.isPending}
                onClick={() => void session.refresh()}
              >
                {t("canonical.refresh")}
              </Button>
              <Button
                onClick={() => {
                  setShowOrganization(false);
                  setShowProfile((open) => !open);
                }}
              >
                {t("canonical.profileDetails")}
              </Button>
              {showsOrganizationDirectory(session.identity.member.role) && (
                <Button
                  onClick={() => {
                    setShowProfile(false);
                    setShowOrganization((open) => !open);
                  }}
                >
                  {t("canonicalOrganization.open")}
                </Button>
              )}
              <Button onClick={() => void session.logout()}>
                {t("canonical.signOut")}
              </Button>
            </Space>
            {showProfile && (
              <CanonicalProfilePanel
                key={JSON.stringify([
                  session.identity.account_id,
                  session.identity.organization.id,
                  session.identity.member.id,
                ])}
                identity={session.identity}
              />
            )}
            {showOrganization &&
              showsOrganizationDirectory(session.identity.member.role) && (
                <CanonicalOrganizationPanel
                  key={JSON.stringify([
                    session.identity.account_id,
                    session.identity.organization.id,
                    session.identity.member.id,
                  ])}
                  identity={session.identity}
                />
              )}
          </Space>
        ) : session.hasAccessToken && !session.isPending ? (
          <Space direction="vertical">
            <CanonicalError error={session.error || new Error()} />
            <Button onClick={session.reload}>{t("canonical.retry")}</Button>
            <Button onClick={() => void session.logout()}>
              {t("canonical.signOut")}
            </Button>
          </Space>
        ) : (
          <CanonicalPhoneLogin
            isAvailable={phoneLoginAvailable}
            isPasswordAvailable={passwordLoginAvailable}
            onAuthenticated={onAuthenticated}
          />
        )}
        {!session.hasAccessToken && alternateLogin && (
          <Typography.Link
            href={alternateLogin.href}
            disabled={session.isPending}
            style={{ display: "block", marginTop: 16 }}
          >
            {alternateLogin.label}
          </Typography.Link>
        )}
      </Card>
    </div>
  );
}

export default function CanonicalLogin({
  phoneLoginAvailable,
  passwordLoginAvailable = false,
  onAuthenticated,
}: {
  phoneLoginAvailable: boolean;
  passwordLoginAvailable?: boolean;
  onAuthenticated?: () => void;
}) {
  return (
    <CanonicalIdentityProvider>
      <CanonicalAccountScreen
        phoneLoginAvailable={phoneLoginAvailable}
        passwordLoginAvailable={passwordLoginAvailable}
        onAuthenticated={onAuthenticated}
      />
    </CanonicalIdentityProvider>
  );
}

/** Ordinary account login uses the original organization password policy. */
export function CanonicalMemberLogin(
  props: Parameters<typeof CanonicalAccountScreen>[0],
) {
  const { t } = useTranslation();
  return (
    <CanonicalIdentityProvider
      passwordLogin={canonicalIdentityApi.passwordLogin}
    >
      <CanonicalAccountScreen
        {...props}
        alternateLogin={{
          href: "/login/moss",
          label: t("canonical.mossSignIn"),
        }}
      />
    </CanonicalIdentityProvider>
  );
}
