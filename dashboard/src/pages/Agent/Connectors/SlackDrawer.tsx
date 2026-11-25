import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Drawer,
  Form,
  Input,
  Select,
  Space,
  Switch,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  connectorsApi,
  type ConnectorCatalogEntry,
  type ConnectorInstance,
} from "../../../api/modules/connectors";
import { apiErrorMessage, isNotFoundApiError } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";

interface Fields {
  display_name: string;
  client_id: string;
  client_secret?: string;
  scopes?: string[];
  default_open: boolean;
}

export function SlackDrawer({
  entry,
  instance,
  onClose,
  onSaved,
}: {
  entry: ConnectorCatalogEntry;
  instance: ConnectorInstance | null;
  onClose: () => void;
  onSaved: (instance: ConnectorInstance) => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<Fields>();
  const [ready, setReady] = useState(!instance);
  const [configured, setConfigured] = useState(false);
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dirty, setDirty] = useState(false);
  const [authorization, setAuthorization] = useState<{
    state_id: string;
    authorize_url: string;
  } | null>(null);
  useEffect(() => {
    if (!authorization) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const deadline = Date.now() + 600_000;
    async function check(stateId: string) {
      try {
        const result = await connectorsApi.oauthPending(stateId);
        if (cancelled) return;
        setConnected(result.applied === true);
        setAuthorization(null);
      } catch (err) {
        if (cancelled) return;
        if (isNotFoundApiError(err) && Date.now() < deadline) {
          timer = setTimeout(() => void check(stateId), 2500);
        } else {
          setError(
            isNotFoundApiError(err)
              ? t("connectors.oauthTimedOut")
              : apiErrorMessage(err, t("connectors.oauthFailed")),
          );
          setAuthorization(null);
        }
      }
    }
    timer = setTimeout(() => void check(authorization.state_id), 2500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [authorization, t]);
  const instanceId = instance?.instance_id;
  useEffect(() => {
    if (!instanceId) return;
    let active = true;
    connectorsApi
      .getInstance(instanceId)
      .then((detail) => {
        if (!active) return;
        const preview = detail.credentials_preview;
        form.setFieldsValue({
          display_name: detail.display_name,
          default_open: detail.default_open,
          client_id: preview.client_id,
          client_secret: "",
          scopes: String(preview.scopes || "")
            .split(" ")
            .filter(Boolean),
        });
        setConfigured(!!preview.client_secret_configured);
        setConnected(!!preview.oauth_configured);
        setReady(true);
      })
      .catch((err) => {
        if (active)
          setError(apiErrorMessage(err, t("connectors.createFailed")));
      });
    return () => {
      active = false;
    };
  }, [instanceId, form, t]);

  const save = async (values: Fields) => {
    setBusy(true);
    setError("");
    try {
      const body = {
        display_name: values.display_name,
        default_open: values.default_open,
        credentials: {
          client_id: values.client_id,
          client_secret: values.client_secret,
          scopes: (values.scopes ?? []).join(" "),
        },
      };
      const saved = instanceId
        ? await connectorsApi.patchInstance(instanceId, body)
        : await connectorsApi.createInstance({ kind: "slack", ...body });
      form.setFieldValue("client_secret", "");
      setConfigured(true);
      setDirty(false);
      if (instanceId)
        setConnected(
          !!(await connectorsApi.getInstance(instanceId)).credentials_preview
            .oauth_configured,
        );
      onSaved(saved);
      message.success(t("connectors.saveSuccess"));
    } catch (err) {
      setError(apiErrorMessage(err, t("connectors.createFailed")));
    } finally {
      setBusy(false);
    }
  };

  const act = async (probe: boolean) => {
    if (!instanceId) return;
    setBusy(true);
    setError("");
    try {
      if (probe) {
        const result = await connectorsApi.testInstance(instanceId);
        if (!result.ok)
          throw new Error(result.error || t("connectors.probeFailed"));
        message.success(
          t("connectors.probeOkTools", { count: result.tool_count ?? 0 }),
        );
      } else {
        const result = await connectorsApi.oauthStart(
          { type: "catalog", kind: "slack", instance_id: instanceId },
          "/connectors",
        );
        setAuthorization(result);
        window.open(result.authorize_url, "_blank", "noopener,noreferrer");
      }
    } catch (err) {
      setError(
        apiErrorMessage(
          err,
          t(probe ? "connectors.probeFailed" : "connectors.oauthStartFailed"),
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  const secure = entry.redirect_uri?.startsWith("https://");
  return (
    <Drawer open onClose={onClose} title="Slack" width={520}>
      <Collapse
        defaultActiveKey={instance ? [] : ["guide"]}
        style={{ marginBottom: 16 }}
        items={[
          {
            key: "guide",
            label: t("connectors.slack.guide.title"),
            children: (
              <>
                <Typography.Link
                  href="https://api.slack.com/apps"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t("connectors.slack.guide.apps")}
                </Typography.Link>
                <ol>
                  <li>{t("connectors.slack.guide.credentials")}</li>
                  <li>{t("connectors.slack.guide.redirect")}</li>
                  <li>{t("connectors.slack.guide.scopes")}</li>
                  <li>{t("connectors.slack.guide.authorize")}</li>
                </ol>
                <Typography.Link
                  href="https://docs.slack.dev/ai/slack-mcp-server/"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t("connectors.slack.guide.reference")}
                </Typography.Link>
              </>
            ),
          },
        ]}
      />
      <Form
        form={form}
        layout="vertical"
        disabled={busy || !ready}
        initialValues={{ display_name: "Slack", default_open: false }}
        onFinish={save}
        onValuesChange={() => {
          setDirty(true);
          setAuthorization(null);
        }}
      >
        <Form.Item
          label={t("connectors.displayName")}
          name="display_name"
          rules={[{ required: true, whitespace: true }]}
        >
          <Input />
        </Form.Item>
        <Form.Item
          label="Client ID"
          name="client_id"
          rules={[{ required: true, whitespace: true }]}
        >
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item
          label="Client Secret"
          name="client_secret"
          rules={[{ required: !configured }]}
        >
          <Input.Password
            autoComplete="new-password"
            placeholder={
              configured ? t("connectors.secretConfigured") : undefined
            }
          />
        </Form.Item>
        <Form.Item
          label="OAuth scopes"
          name="scopes"
          extra={t("connectors.slack.scopeHint")}
          rules={[{ required: true, type: "array", min: 1 }]}
        >
          <Select mode="tags" tokenSeparators={[" ", ","]} />
        </Form.Item>
        <Form.Item label="MCP URL">
          <Typography.Text copyable>{entry.mcp_url}</Typography.Text>
        </Form.Item>
        <Form.Item
          label={t("connectors.slack.callback")}
          extra={t("connectors.slack.register")}
        >
          <Typography.Text copyable>{entry.redirect_uri}</Typography.Text>
        </Form.Item>
        <Form.Item
          label={t("connectors.defaultOpen")}
          name="default_open"
          valuePropName="checked"
        >
          <Switch />
        </Form.Item>
        <Space direction="vertical">
          {!secure && (
            <Alert
              type="warning"
              showIcon
              message={t("connectors.slack.https")}
            />
          )}
          {error && <Alert type="error" showIcon message={error} />}
          {authorization && (
            <Typography.Link
              href={authorization.authorize_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              {t("connectors.openAuthorizePage")}
            </Typography.Link>
          )}
          <Typography.Text>
            {t(
              connected
                ? "connectors.slack.connected"
                : "connectors.slack.pending",
            )}
          </Typography.Text>
          <Space wrap>
            <Button htmlType="submit" type="primary">
              {t("common.save")}
            </Button>
            <Button
              disabled={!instanceId || dirty || !secure}
              onClick={() => void act(false)}
            >
              {t(
                connected
                  ? "connectors.slack.reauthorize"
                  : "connectors.goToAuthorize",
              )}
            </Button>
            <Button
              disabled={!connected || dirty}
              onClick={() => void act(true)}
            >
              {t("connectors.probe")}
            </Button>
          </Space>
        </Space>
      </Form>
    </Drawer>
  );
}
