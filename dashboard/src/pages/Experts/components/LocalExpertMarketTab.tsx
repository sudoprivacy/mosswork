import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Empty, Input, Spin, Tag } from "antd";
import { useTranslation } from "react-i18next";
import { GraduationCap, RefreshCw } from "lucide-react";
import {
  localExpertMarketApi,
  type LocalMarketExpert,
} from "../../../api/modules/localExpertMarket";
import { apiErrorMessage } from "../../../utils/apiError";
import styles from "../index.module.less";

export default function LocalExpertMarketTab({
  onCreate,
}: {
  onCreate: (expert: LocalMarketExpert) => void;
}) {
  const { t } = useTranslation();
  const [items, setItems] = useState<LocalMarketExpert[]>([]);
  const [canCreate, setCanCreate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [iconUrls, setIconUrls] = useState<Record<string, string>>({});
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await localExpertMarketApi.list();
      setItems(data.items);
      setCanCreate(data.can_create);
    } catch (err) {
      setError(apiErrorMessage(err, t("experts.localMarket.loadFailed"), t));
    } finally {
      setLoading(false);
    }
  }, [t]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    let cancelled = false;
    const objectUrls: string[] = [];
    setIconUrls({});
    void Promise.all(
      items.map(async (expert) => {
        if (!expert.icon_url) return null;
        try {
          const blob = await localExpertMarketApi.icon(expert.version_id);
          const objectUrl = URL.createObjectURL(blob);
          objectUrls.push(objectUrl);
          return [expert.version_id, objectUrl] as const;
        } catch {
          return null;
        }
      }),
    ).then((entries) => {
      if (cancelled) return;
      setIconUrls(
        Object.fromEntries(
          entries.filter(
            (entry): entry is readonly [string, string] => entry !== null,
          ),
        ),
      );
    });
    return () => {
      cancelled = true;
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [items]);
  const query = search.trim().toLowerCase();
  const visible = items.filter((item) =>
    `${item.name} ${item.description}`.toLowerCase().includes(query),
  );

  return (
    <div className={styles.marketContainer}>
      <div
        style={{
          display: "flex",
          gap: 12,
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <Input.Search
          style={{ maxWidth: 360 }}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t("experts.localMarket.search")}
          allowClear
        />
        <Button
          icon={<RefreshCw size={14} />}
          onClick={() => void load()}
          loading={loading}
        >
          {t("common.refresh")}
        </Button>
      </div>
      <p className={styles.expertCardHint}>{t("experts.localMarket.hint")}</p>
      {!canCreate && !loading && !error && (
        <Alert
          type="info"
          showIcon
          message={t("experts.localMarket.assignedOnly")}
        />
      )}
      {error ? (
        <Alert type="error" showIcon message={error} />
      ) : loading ? (
        <Spin />
      ) : visible.length === 0 ? (
        <Empty description={t("experts.localMarket.empty")} />
      ) : (
        <div className={styles.cardGrid}>
          {visible.map((expert) => (
            <div
              className={styles.expertTemplateCard}
              key={expert.version_id}
              role="button"
              tabIndex={0}
              onClick={() => {
                if (canCreate) onCreate(expert);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  if (canCreate) onCreate(expert);
                }
              }}
            >
              <div className={styles.expertTemplateHeader}>
                <div className={styles.agentCardIcon}>
                  {iconUrls[expert.version_id] ? (
                    <img
                      src={iconUrls[expert.version_id]}
                      alt=""
                      style={{
                        width: 28,
                        height: 28,
                        objectFit: "cover",
                        borderRadius: 8,
                      }}
                    />
                  ) : (
                    <GraduationCap size={22} />
                  )}
                </div>
                <div className={styles.agentCardTitleBlock}>
                  <div className={styles.agentCardName}>{expert.name}</div>
                  <Tag>v{expert.version}</Tag>
                </div>
              </div>
              <div className={styles.agentCardDesc}>{expert.description}</div>
              <div className={styles.expertCardFooter}>
                <span className={styles.expertCardHint}>
                  {t("experts.localMarket.dependencies", {
                    count: expert.dependencies.length,
                  })}
                </span>
                <Button
                  type="primary"
                  size="small"
                  disabled={!canCreate}
                  onClick={(event) => {
                    event.stopPropagation();
                    onCreate(expert);
                  }}
                >
                  {t("experts.createFromTemplate")}
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
