import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Drawer,
  Empty,
  Input,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Package, RefreshCw } from "lucide-react";
import { useCurrentUser } from "../hooks/useCurrentUser";
import { userCan } from "../utils/permissions";
import { apiErrorMessage } from "../utils/apiError";
import { message } from "../utils/antdMessage";
import {
  localSkillMarketApi,
  type LocalSkillDetail,
  type LocalSkillRelease,
} from "../api/modules/localSkillMarket";
import styles from "./ResourceMarketEntry.module.less";
import MarketIcon from "./MarketIcon";

type Props = {
  initialVersionId?: string | null;
  onInstalled?: (packageId: string) => void;
};

/** Local catalog consumption is independent of market publication permissions. */
export default function ResourceMarketEntry(props: Props) {
  const { t } = useTranslation();
  const user = useCurrentUser();
  if (!userCan(user, "skill_packages")) {
    return <Empty description={t("resourceMarket.contactAdmin")} />;
  }
  return <LocalSkillCatalog {...props} />;
}

function LocalSkillCatalog({ initialVersionId, onInstalled }: Props) {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const [items, setItems] = useState<LocalSkillRelease[]>([]);
  const itemsRef = useRef<LocalSkillRelease[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [versionId, setVersionId] = useState<string | null>(
    initialVersionId ?? null,
  );
  const [detail, setDetail] = useState<LocalSkillDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [packageName, setPackageName] = useState("");
  const [installing, setInstalling] = useState(false);
  const loadSequence = useRef(0);
  const load = useCallback(async (append = false) => {
    const sequence = ++loadSequence.current;
    setLoading(true);
    setError("");
    try {
      const offset = append ? itemsRef.current.length : 0;
      const result = await localSkillMarketApi.list(offset);
      if (loadSequence.current === sequence) {
        const nextItems = append
          ? [...itemsRef.current, ...result.items]
          : result.items;
        itemsRef.current = nextItems;
        setItems(nextItems);
        setNextOffset(result.next_offset ?? null);
      }
    } catch (err) {
      if (loadSequence.current === sequence)
        setError(apiErrorMessage(err, t("localSkillMarket.loadFailed"), t));
    } finally {
      if (loadSequence.current === sequence) setLoading(false);
    }
  }, [t]);
  useEffect(() => {
    void load();
    return () => {
      loadSequence.current += 1;
    };
  }, [load]);
  useEffect(() => {
    if (initialVersionId) setVersionId(initialVersionId);
  }, [initialVersionId]);
  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setDetailError("");
    if (!versionId) return;
    setDetailLoading(true);
    localSkillMarketApi
      .detail(versionId)
      .then((result) => {
        if (cancelled) return;
        setDetail(result);
        setPackageName(`${result.name} v${result.version}`);
      })
      .catch((err) => {
        if (!cancelled)
          setDetailError(
            apiErrorMessage(err, t("localSkillMarket.loadFailed"), t),
          );
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [versionId, t]);

  const install = async () => {
    if (!detail || !packageName.trim()) return;
    setInstalling(true);
    setDetailError("");
    try {
      const result = await localSkillMarketApi.install(
        detail.version_id,
        packageName.trim(),
      );
      message.success(
        t("localSkillMarket.installed", { name: result.package.name }),
      );
      setVersionId(null);
      onInstalled?.(result.package.id);
    } catch (err) {
      setDetailError(
        apiErrorMessage(err, t("localSkillMarket.installFailed"), t),
      );
    } finally {
      setInstalling(false);
    }
  };
  const search = query.trim().toLocaleLowerCase();
  const visible = items.filter((item) =>
    `${item.name} ${item.description}`.toLocaleLowerCase().includes(search),
  );
  return (
    <div className={styles.container}>
      <Alert type="info" showIcon message={t("localSkillMarket.hint")} />
      <div className={styles.toolbar}>
        <Input.Search
          aria-label={t("localSkillMarket.search")}
          placeholder={t("localSkillMarket.search")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          allowClear
        />
        <Button
          icon={<RefreshCw size={14} />}
          onClick={() => void load()}
          loading={loading}
        >
          {t("common.refresh")}
        </Button>
        {userCan(user, "resource_market") ? (
          <Link to="/resource-market">{t("localSkillMarket.manage")}</Link>
        ) : null}
      </div>
      {error ? <Alert type="error" showIcon message={error} /> : null}
      {loading ? (
        <Spin />
      ) : !error && visible.length === 0 ? (
        <Empty description={t("localSkillMarket.empty")} />
      ) : (
        <div className={styles.grid}>
          {visible.map((item) => (
            <Card
              key={item.resource_id}
              size="small"
              title={
                <Space>
                  <MarketIcon
                    src={item.icon_url}
                    size={28}
                    fallback={<Package size={18} />}
                  />
                  <span>{item.name}</span>
                </Space>
              }
              extra={<Tag>v{item.version}</Tag>}
            >
              <Typography.Paragraph type="secondary" ellipsis={{ rows: 3 }}>
                {item.description}
              </Typography.Paragraph>
              <Button onClick={() => setVersionId(item.version_id)}>
                {t("localSkillMarket.details")}
              </Button>
            </Card>
          ))}
        </div>
      )}
      {!loading && !error && nextOffset !== null ? (
        <Button loading={loading} onClick={() => void load(true)}>
          {t("resourceMarket.loadMore")}
        </Button>
      ) : null}
      <Drawer
        title={detail?.name ?? t("localSkillMarket.details")}
        open={Boolean(versionId)}
        width={760}
        closable={!installing}
        maskClosable={!installing}
        onClose={() => setVersionId(null)}
        footer={
          <Button
            type="primary"
            loading={installing}
            disabled={!detail || detailLoading || !packageName.trim()}
            onClick={() => void install()}
          >
            {t("localSkillMarket.install")}
          </Button>
        }
      >
        {detailLoading ? <Spin /> : null}
        {detailError ? (
          <Alert type="error" showIcon message={detailError} />
        ) : null}
        {detail ? (
          <Space direction="vertical" size="middle" className={styles.details}>
            <Typography.Paragraph>{detail.description}</Typography.Paragraph>
            <label className={styles.field}>
              <span>{t("resourceMarket.version")}</span>
              <Select
                aria-label={t("resourceMarket.version")}
                value={detail.version_id}
                disabled={installing}
                onChange={setVersionId}
                options={detail.versions.map((version) => ({
                  value: version.version_id,
                  label: `v${version.version}`,
                }))}
              />
            </label>
            <Typography.Text>{detail.changelog}</Typography.Text>
            <Typography.Text type="secondary" copyable>
              {detail.object_sha256}
            </Typography.Text>
            <Typography.Text strong>
              {t("localSkillMarket.dependencies")}
            </Typography.Text>
            {detail.lock.map((entry) => (
              <Typography.Text key={entry.resource_id}>
                {entry.resource_id} · v{entry.version}
              </Typography.Text>
            ))}
            <Collapse
              items={detail.file_contents.map((file) => ({
                key: file.name,
                label: file.name,
                children: <pre className={styles.preview}>{file.content}</pre>,
              }))}
            />
            <Alert
              type="info"
              showIcon
              message={t("localSkillMarket.snapshotHint")}
            />
            <label className={styles.field}>
              <span>{t("localSkillMarket.packageName")}</span>
              <Input
                aria-label={t("localSkillMarket.packageName")}
                value={packageName}
                maxLength={200}
                disabled={installing}
                onChange={(event) => setPackageName(event.target.value)}
              />
            </label>
          </Space>
        ) : null}
      </Drawer>
    </div>
  );
}
