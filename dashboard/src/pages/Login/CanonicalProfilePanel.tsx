import { useEffect, useState } from "react";
import {
  Alert,
  Avatar,
  Button,
  Form,
  Input,
  Space,
  Spin,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import type { CanonicalIdentity } from "../../api/modules/canonicalIdentity";
import { useCanonicalProfile } from "../../hooks/useCanonicalProfile";
import { parseApiError } from "../../utils/apiError";

/** Basic self-profile UI; it neither enables runtime resources nor grants access. */
export function CanonicalProfilePanel({
  identity,
}: {
  identity: CanonicalIdentity;
}) {
  const { t } = useTranslation();
  const state = useCanonicalProfile(identity);
  const [nickname, setNickname] = useState("");
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    setNickname(state.profile?.nickname ?? "");
  }, [state.profile]);
  const code = parseApiError(state.error)?.code;
  const localKey =
    state.error instanceof Error &&
    state.error.message.startsWith("canonical.profile")
      ? state.error.message
      : "";
  const key = localKey || (code ? `apiErrors.${code}` : "canonical.error");
  const errorText = t(key) === key ? t("canonical.error") : t(key);
  const disabled = state.isPending || state.isLoading;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Typography.Title level={4}>
        {t("canonical.profileTitle")}
      </Typography.Title>
      {state.isLoading && <Spin />}
      {state.error != null && (
        <Alert type="error" showIcon message={errorText} />
      )}
      {notice && <Alert type="success" message={t(notice)} />}
      {state.profile && (
        <>
          <Typography.Text>
            {t("canonical.loginName")}: {state.profile.login_name}
          </Typography.Text>
          <Form
            layout="vertical"
            onFinish={async () => {
              setNotice("");
              if (await state.saveNickname(nickname))
                setNotice("canonical.profileSaved");
            }}
          >
            <Form.Item label={t("canonical.nickname")}>
              <Input
                aria-label={t("canonical.nickname")}
                value={nickname}
                maxLength={60}
                disabled={disabled}
                onChange={(event) => setNickname(event.target.value)}
              />
            </Form.Item>
            <Button
              htmlType="submit"
              disabled={disabled || !nickname.trim()}
              loading={state.isPending}
            >
              {t("canonical.saveNickname")}
            </Button>
          </Form>
          <Space wrap>
            <Avatar src={state.profile.avatar ?? undefined} size={64} />
            <Input
              type="file"
              accept="image/png,image/jpeg,image/webp"
              aria-label={t("canonical.avatarFile")}
              disabled={disabled}
              onChange={async (event) => {
                const input = event.currentTarget;
                const file = input.files?.item(0);
                if (!file) return;
                setNotice("");
                if (await state.saveAvatar(file))
                  setNotice("canonical.profileSaved");
                input.value = "";
              }}
            />
            <Button
              disabled={disabled || !state.profile.avatar}
              onClick={async () => {
                setNotice("");
                if (await state.saveAvatar(null))
                  setNotice("canonical.profileSaved");
              }}
            >
              {t("canonical.clearAvatar")}
            </Button>
          </Space>
          {state.profile.can_change_password && (
            <Form
              layout="vertical"
              onFinish={async () => {
                setNotice("");
                try {
                  if (await state.changePassword(oldPassword, newPassword))
                    setNotice("canonical.passwordSaved");
                } finally {
                  setOldPassword("");
                  setNewPassword("");
                }
              }}
            >
              <Form.Item label={t("canonical.oldPassword")}>
                <Input.Password
                  aria-label={t("canonical.oldPassword")}
                  autoComplete="current-password"
                  value={oldPassword}
                  disabled={disabled}
                  onChange={(event) => setOldPassword(event.target.value)}
                />
              </Form.Item>
              <Form.Item label={t("canonical.newPassword")}>
                <Input.Password
                  aria-label={t("canonical.newPassword")}
                  autoComplete="new-password"
                  maxLength={20}
                  value={newPassword}
                  disabled={disabled}
                  onChange={(event) => setNewPassword(event.target.value)}
                />
              </Form.Item>
              <Button
                htmlType="submit"
                disabled={disabled || !oldPassword || !newPassword}
                loading={state.isPending}
              >
                {t("canonical.savePassword")}
              </Button>
            </Form>
          )}
        </>
      )}
      <Button
        disabled={state.isPending}
        onClick={() => {
          setNotice("");
          void state.reload();
        }}
      >
        {t("canonical.profileReload")}
      </Button>
    </Space>
  );
}
