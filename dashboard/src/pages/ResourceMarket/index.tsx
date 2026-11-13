import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { Bot, Download, Package, Plus, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { userCan } from "../../utils/permissions";
import { authApi } from "../../api/modules/auth";
import {
  resourceMarketApi,
  type MarketDetail,
  type MarketDraft,
  type MarketResource,
  type MarketVersion,
} from "../../api/modules/resourceMarket";
import PageShell from "../../layouts/PageShell";
import { apiErrorMessage, parseApiError } from "../../utils/apiError";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import MarketIcon from "../../components/MarketIcon";
import styles from "./index.module.less";

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function ResourceMarketPage() {
  const { t } = useTranslation();
  const currentUser = useCurrentUser();
  const { message } = App.useApp();
  const timezone = useServerTimezone();
  const [resources, setResources] = useState<MarketResource[]>([]);
  const [admin, setAdmin] = useState(false);
  const [userId, setUserId] = useState<number>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("all");
  const [selected, setSelected] = useState<string>();
  const [detail, setDetail] = useState<MarketDetail>();
  const [draftId, setDraftId] = useState<string>();
  const [activeTab, setActiveTab] = useState("versions");
  const [preview, setPreview] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [importTarget, setImportTarget] = useState<
    MarketResource | null | undefined
  >();
  const [file, setFile] = useState<File>();
  const [decision, setDecision] = useState<"publish" | "reject">();
  const [reviewNote, setReviewNote] = useState("");
  const detailSequence = useRef(0);
  const previewSequence = useRef(0);
  const listSequence = useRef(0);
  const [hasMore, setHasMore] = useState(false);

  const report = useCallback(
    (error: unknown) => {
      const parsed = parseApiError(error);
      const reason = parsed?.details?.reason;
      const reasonKey = `resourceMarket.validation.${String(reason)}`;
      const text =
        reason && t(reasonKey) !== reasonKey
          ? t(reasonKey)
          : apiErrorMessage(error, t("resourceMarket.failed"), t);
      void message.error(text);
    },
    [message, t],
  );

  const reload = useCallback(async () => {
    const sequence = ++listSequence.current;
    const rows = await resourceMarketApi.list();
    if (sequence !== listSequence.current) return;
    setResources(rows);
    setHasMore(rows.length === 100);
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    const sequence = ++detailSequence.current;
    const value = await resourceMarketApi.detail(id);
    if (sequence !== detailSequence.current) return;
    setDetail(value);
    setDraftId((current) =>
      value.drafts.some((item) => item.draft_id === current)
        ? current
        : value.drafts[0]?.draft_id,
    );
  }, []);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([authApi.me(), resourceMarketApi.list()])
      .then(([user, rows]) => {
        if (cancelled) return;
        setAdmin(user.role === "admin");
        setUserId(user.id);
        setResources(rows);
        setHasMore(rows.length === 100);
      })
      .catch(report)
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [report]);

  useEffect(() => {
    setDetail(undefined);
    setDraftId(undefined);
    setPreview("");
    setDecision(undefined);
    if (selected) void loadDetail(selected).catch(report);
    return () => {
      detailSequence.current += 1;
      previewSequence.current += 1;
    };
  }, [selected, loadDetail, report]);

  useEffect(() => {
    previewSequence.current += 1;
    setPreview("");
    setPreviewLoading(false);
  }, [draftId]);

  const execute = async (
    operation: () => Promise<unknown>,
    resourceId = selected,
  ) => {
    setBusy(true);
    try {
      await operation();
      await Promise.all([
        reload(),
        ...(resourceId ? [loadDetail(resourceId)] : []),
      ]);
      void message.success(t("resourceMarket.success"));
    } catch (error) {
      report(error);
    } finally {
      setBusy(false);
    }
  };

  const loadPreview = async (draft: MarketDraft, path: string) => {
    const sequence = ++previewSequence.current;
    setPreview("");
    setPreviewLoading(true);
    try {
      const result = await resourceMarketApi.preview(draft.draft_id, path);
      if (sequence === previewSequence.current) setPreview(result.content);
    } catch (error) {
      if (sequence === previewSequence.current) report(error);
    } finally {
      if (sequence === previewSequence.current) setPreviewLoading(false);
    }
  };

  const importFile = async () => {
    if (!file) return;
    setBusy(true);
    try {
      const draft = await resourceMarketApi.import(
        file,
        importTarget?.resource_id,
      );
      await reload();
      setImportTarget(undefined);
      setFile(undefined);
      if (selected === draft.resource_id) await loadDetail(draft.resource_id);
      else setSelected(draft.resource_id);
      setDraftId(draft.draft_id);
      setActiveTab("drafts");
      void message.success(t("resourceMarket.imported"));
    } catch (error) {
      report(error);
    } finally {
      setBusy(false);
    }
  };

  const download = async (operation: () => Promise<Blob>, name: string) => {
    try {
      saveBlob(await operation(), name);
    } catch (error) {
      report(error);
    }
  };

  const visible = resources.filter(
    (item) =>
      (kind === "all" || item.kind === kind) &&
      `${item.name} ${item.description} ${item.slug}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const draft = detail?.drafts.find((item) => item.draft_id === draftId);
  const canManage =
    detail && (admin || detail.resource.owner_user_id === userId);

  const draftContent = draft && (
    <div className={styles.stack}>
      <Select
        aria-label={t("resourceMarket.selectDraft")}
        value={draftId}
        onChange={setDraftId}
        options={detail?.drafts.map((item) => ({
          value: item.draft_id,
          label: `${item.version} · ${t(
            `resourceMarket.states.${item.state}`,
          )} · ${item.draft_id.slice(-6)}`,
        }))}
      />
      <Descriptions
        size="small"
        column={1}
        bordered
        items={[
          {
            key: "state",
            label: t("resourceMarket.status"),
            children: t(`resourceMarket.states.${draft.state}`),
          },
          {
            key: "sha",
            label: "SHA-256",
            children: (
              <Typography.Text copyable style={{ wordBreak: "break-all" }}>
                {draft.object_sha256}
              </Typography.Text>
            ),
          },
          {
            key: "files",
            label: t("resourceMarket.files"),
            children: `${draft.file_count} · ${(
              draft.total_bytes / 1024
            ).toFixed(1)} KiB`,
          },
          {
            key: "runtime",
            label: t("resourceMarket.runtime"),
            children:
              draft.manifest.runtime.execution_targets
                .map((target) => t(`resourceMarket.targets.${target}`))
                .join(" / ") +
              " · " +
              draft.manifest.runtime.platforms.join(", "),
          },
          {
            key: "changes",
            label: t("resourceMarket.changelog"),
            children: draft.manifest.changelog,
          },
          {
            key: "deps",
            label: t("resourceMarket.dependencies"),
            children: draft.manifest.dependencies.length
              ? draft.manifest.dependencies
                  .map((dep) => `${dep.resource_id}@${dep.version}`)
                  .join(", ")
              : t("resourceMarket.none"),
          },
          {
            key: "config",
            label: t("resourceMarket.configuration"),
            children:
              Object.entries(draft.manifest.configuration)
                .map(
                  ([key, value]) =>
                    `${key} (${value.type}${
                      value.required ? `, ${t("resourceMarket.required")}` : ""
                    })`,
                )
                .join(", ") || t("resourceMarket.none"),
          },
          ...(draft.review_note
            ? [
                {
                  key: "note",
                  label: t("resourceMarket.reviewNote"),
                  children: draft.review_note,
                },
              ]
            : []),
        ]}
      />
      <Select
        aria-label={t("resourceMarket.preview")}
        placeholder={t("resourceMarket.preview")}
        key={draft.draft_id}
        onChange={(path) => void loadPreview(draft, path)}
        options={draft.files.map((item) => ({
          value: item.path,
          label: `${item.path} (${item.size} B)`,
        }))}
      />
      <Spin spinning={previewLoading}>
        <pre className={styles.preview}>
          {preview || t("resourceMarket.previewHint")}
        </pre>
      </Spin>
      <Space wrap>
        {draft.state === "draft" && (
          <Button
            type="primary"
            loading={busy}
            onClick={() => void execute(() => resourceMarketApi.submit(draft))}
          >
            {t("resourceMarket.submit")}
          </Button>
        )}
        {admin && draft.state === "submitted" && (
          <>
            <Button
              type="primary"
              disabled={busy}
              onClick={() => {
                setDecision("publish");
                setReviewNote("");
              }}
            >
              {t("resourceMarket.publish")}
            </Button>
            <Button
              danger
              disabled={busy}
              onClick={() => {
                setDecision("reject");
                setReviewNote("");
              }}
            >
              {t("resourceMarket.reject")}
            </Button>
          </>
        )}
      </Space>
    </div>
  );

  return (
    <PageShell
      title={t("resourceMarket.title")}
      subtitle={t("resourceMarket.subtitle")}
      actions={
        <Space wrap>
          <Button
            icon={<RefreshCw size={15} />}
            disabled={busy}
            onClick={() => void execute(async () => undefined)}
          >
            {t("resourceMarket.refresh")}
          </Button>
          <Button
            type="primary"
            icon={<Plus size={15} />}
            onClick={() => {
              setImportTarget(null);
              setFile(undefined);
            }}
          >
            {t("resourceMarket.import")}
          </Button>
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        message={t("resourceMarket.flow")}
        description={t("resourceMarket.scope")}
      />
      <div className={styles.toolbar}>
        <Segmented
          value={kind}
          onChange={(value) => setKind(String(value))}
          options={[
            { value: "all", label: t("resourceMarket.all") },
            { value: "expert", label: t("resourceMarket.expert") },
            { value: "skill", label: t("resourceMarket.skill") },
          ]}
        />
        <Input.Search
          style={{ maxWidth: 300 }}
          allowClear
          placeholder={t("resourceMarket.search")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <Spin spinning={loading}>
        {!loading && visible.length === 0 ? (
          <Empty description={t("resourceMarket.empty")} />
        ) : (
          <div className={styles.grid}>
            {visible.map((item) => (
              <button
                type="button"
                key={item.resource_id}
                className={styles.card}
                onClick={() => {
                  setActiveTab("versions");
                  setSelected(item.resource_id);
                }}
              >
                <div className={styles.cardHead}>
                  <MarketIcon
                    src={item.icon_url}
                    size={40}
                    fallback={
                      item.kind === "expert" ? (
                        <Bot size={22} />
                      ) : (
                        <Package size={22} />
                      )
                    }
                  />
                  <span>{item.name}</span>
                </div>
                <div className={styles.description}>{item.description}</div>
                <div className={styles.meta}>
                  <Space>
                    <Tag>{t(`resourceMarket.${item.kind}`)}</Tag>
                    <Tag
                      color={
                        !item.listed
                          ? "default"
                          : item.recommended_version
                          ? "green"
                          : "orange"
                      }
                    >
                      {!item.listed
                        ? t("resourceMarket.unlisted")
                        : item.recommended_version
                        ? `v${item.recommended_version}`
                        : t("resourceMarket.unpublished")}
                    </Tag>
                  </Space>
                  <span>
                    {t("resourceMarket.versionCount", {
                      count: item.version_count,
                    })}
                  </span>
                </div>
              </button>
            ))}
          </div>
        )}
      </Spin>
      {hasMore && (
        <Button
          loading={busy}
          onClick={() => {
            setBusy(true);
            void resourceMarketApi
              .list(resources.length)
              .then((rows) => {
                setResources((current) => [...current, ...rows]);
                setHasMore(rows.length === 100);
              })
              .catch(report)
              .finally(() => setBusy(false));
          }}
        >
          {t("resourceMarket.loadMore")}
        </Button>
      )}
      <Drawer
        title={detail?.resource.name || t("resourceMarket.details")}
        open={Boolean(selected)}
        width={760}
        onClose={() => {
          if (!busy) setSelected(undefined);
        }}
      >
        {!detail ? (
          <Spin />
        ) : (
          <div className={styles.stack}>
            <Typography.Paragraph type="secondary">
              {detail.resource.description}
            </Typography.Paragraph>
            <Typography.Text copyable>
              {detail.resource.resource_id}
            </Typography.Text>
            <Space wrap>
              {canManage && (
                <Button
                  disabled={busy}
                  onClick={() => {
                    setImportTarget(detail.resource);
                    setFile(undefined);
                  }}
                >
                  {t("resourceMarket.importVersion")}
                </Button>
              )}
              {admin && (
                <Button
                  disabled={busy}
                  onClick={() =>
                    void execute(() =>
                      resourceMarketApi.listing(
                        detail.resource.resource_id,
                        !detail.resource.listed,
                      ),
                    )
                  }
                >
                  {t(
                    detail.resource.listed
                      ? "resourceMarket.unlist"
                      : "resourceMarket.relist",
                  )}
                </Button>
              )}
            </Space>
            <Tabs
              items={[
                {
                  key: "versions",
                  label: t("resourceMarket.versions"),
                  children: (
                    <Table<MarketVersion>
                      rowKey="version_id"
                      dataSource={detail.versions}
                      pagination={false}
                      scroll={{ x: 540 }}
                      columns={[
                        {
                          title: t("resourceMarket.version"),
                          dataIndex: "version",
                          render: (value: string, row) => (
                            <Space>
                              <strong>{value}</strong>
                              {row.version_id ===
                                detail.resource.recommended_version_id && (
                                <Tag color="green">
                                  {t("resourceMarket.recommended")}
                                </Tag>
                              )}
                            </Space>
                          ),
                        },
                        {
                          title: t("resourceMarket.publishedAt"),
                          dataIndex: "published_at",
                          render: (value: number) =>
                            formatServerDateTime(value, timezone),
                        },
                        {
                          title: t("resourceMarket.actions"),
                          render: (_, row) => (
                            <Space>
                              {detail.resource.kind === "skill" &&
                              detail.resource.listed &&
                              userCan(currentUser, "skill_packages") ? (
                                <Link
                                  to={`/skill-packages?marketVersion=${encodeURIComponent(
                                    row.version_id,
                                  )}`}
                                >
                                  <Button size="small">
                                    {t("localSkillMarket.install")}
                                  </Button>
                                </Link>
                              ) : null}
                              <Button
                                size="small"
                                icon={<Download size={13} />}
                                onClick={() =>
                                  void download(
                                    () =>
                                      resourceMarketApi.download(
                                        row.version_id,
                                      ),
                                    `${detail.resource.slug}-${row.version}.zip`,
                                  )
                                }
                              >
                                {t("resourceMarket.download")}
                              </Button>
                              {admin &&
                                row.version_id !==
                                  detail.resource.recommended_version_id && (
                                  <Button
                                    size="small"
                                    disabled={busy}
                                    onClick={() =>
                                      void execute(() =>
                                        resourceMarketApi.recommend(
                                          detail.resource,
                                          row.version_id,
                                        ),
                                      )
                                    }
                                  >
                                    {t("resourceMarket.setRecommended")}
                                  </Button>
                                )}
                            </Space>
                          ),
                        },
                      ]}
                      expandable={{
                        expandedRowRender: (row) => (
                          <div className={styles.stack}>
                            <span>{row.manifest.changelog}</span>
                            <Typography.Text
                              copyable
                              style={{ wordBreak: "break-all" }}
                            >
                              {row.object_sha256}
                            </Typography.Text>
                          </div>
                        ),
                      }}
                    />
                  ),
                },
                ...(canManage
                  ? [
                      {
                        key: "drafts",
                        label: t("resourceMarket.drafts"),
                        children: draftContent || (
                          <Empty description={t("resourceMarket.noDrafts")} />
                        ),
                      },
                    ]
                  : []),
              ]}
              activeKey={activeTab}
              onChange={setActiveTab}
            />
          </div>
        )}
      </Drawer>
      <Modal
        title={t(
          importTarget
            ? "resourceMarket.importVersion"
            : "resourceMarket.import",
        )}
        open={importTarget !== undefined}
        onCancel={() => {
          if (!busy) setImportTarget(undefined);
        }}
        confirmLoading={busy}
        onOk={() => void importFile()}
        okText={t("resourceMarket.createDraft")}
        okButtonProps={{ disabled: !file }}
        cancelText={t("resourceMarket.cancel")}
      >
        <div className={styles.stack}>
          <Alert type="info" message={t("resourceMarket.packageHint")} />
          {importTarget && (
            <Typography.Text>
              {importTarget.name} · {importTarget.slug}
            </Typography.Text>
          )}
          <label>
            {t("resourceMarket.chooseFile")}
            <input
              key={importTarget?.resource_id || "new"}
              className={styles.upload}
              type="file"
              accept=".zip,application/zip"
              aria-label={t("resourceMarket.chooseFile")}
              disabled={busy}
              onChange={(event) => {
                const value = event.target.files?.[0];
                if (value && value.size > 64 * 1024 * 1024) {
                  setFile(undefined);
                  void message.error(t("resourceMarket.validation.size_limit"));
                } else setFile(value);
              }}
            />
          </label>
          <Space wrap>
            <Button
              onClick={() =>
                void download(
                  () => resourceMarketApi.example("skill"),
                  "market-skill-example.zip",
                )
              }
            >
              {t("resourceMarket.skillTemplate")}
            </Button>
            <Button
              onClick={() =>
                void download(
                  () => resourceMarketApi.example("expert"),
                  "market-expert-example.zip",
                )
              }
            >
              {t("resourceMarket.expertTemplate")}
            </Button>
          </Space>
        </div>
      </Modal>
      <Modal
        title={t(
          decision === "publish"
            ? "resourceMarket.publish"
            : "resourceMarket.reject",
        )}
        open={Boolean(decision)}
        confirmLoading={busy}
        okButtonProps={{
          disabled: !draft || (decision === "reject" && !reviewNote.trim()),
        }}
        onCancel={() => {
          if (!busy) setDecision(undefined);
        }}
        okText={t("resourceMarket.confirm")}
        cancelText={t("resourceMarket.cancel")}
        onOk={() => {
          if (!draft || !decision) return;
          void execute(async () => {
            await resourceMarketApi.review(draft, decision, reviewNote);
            setDecision(undefined);
          });
        }}
      >
        <div className={styles.stack}>
          <Alert type="warning" message={t("resourceMarket.reviewHint")} />
          <Input.TextArea
            aria-label={t("resourceMarket.reviewNote")}
            placeholder={t("resourceMarket.reviewNote")}
            rows={3}
            maxLength={4000}
            value={reviewNote}
            onChange={(event) => setReviewNote(event.target.value)}
          />
        </div>
      </Modal>
    </PageShell>
  );
}
