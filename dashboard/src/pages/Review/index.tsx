import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Empty, Space, Spin, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
import { AUTH_TOKEN_CHANGED_EVENT, getAuthToken } from "../../api/request";
import {
  humanReviewApi,
  ReviewDocumentError,
  type ReviewTarget,
  type ServedReviewDocument,
} from "../../api/modules/humanReview";
import styles from "./index.module.less";
import { HumanReviewFrame } from "./HumanReviewFrame";

interface VisibleReview {
  target: string;
  token: string;
  document: ServedReviewDocument;
}

export function HumanReviewPanel({ projectId, reviewId }: ReviewTarget) {
  const { t } = useTranslation();
  const target = `${projectId}/${reviewId}`;
  const [visible, setVisible] = useState<VisibleReview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [captureAttempted, setCaptureAttempted] = useState(false);
  const [authGeneration, setAuthGeneration] = useState(0);
  const active = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const pending = useRef(false);
  const capturedTarget = useRef<string | null>(null);
  const lastTarget = useRef(target);
  const withdrawRead = useCallback(() => {
    active.current?.abort();
    sequence.current++;
    pending.current = false;
    setVisible(null);
    setBusy(false);
    setError("humanReview.unavailable");
  }, []);

  const load = useCallback(
    async (mode: "restore" | "capture") => {
      const selected = `${projectId}/${reviewId}`;
      if (
        pending.current ||
        (mode === "capture" && capturedTarget.current === selected)
      )
        return;
      if (mode === "capture") {
        capturedTarget.current = selected;
        setCaptureAttempted(true);
      }
      pending.current = true;
      const epoch = ++sequence.current;
      const controller = new AbortController();
      active.current = controller;
      const token = getAuthToken();
      setVisible(null);
      setBusy(true);
      setError("");
      try {
        const document = await humanReviewApi[mode](
          { projectId, reviewId },
          controller.signal,
        );
        if (
          epoch === sequence.current &&
          !controller.signal.aborted &&
          token === getAuthToken()
        ) {
          setVisible({ target: selected, token, document });
        }
      } catch (failure) {
        if (epoch === sequence.current && !controller.signal.aborted) {
          setError(
            failure instanceof ReviewDocumentError
              ? failure.message
              : "humanReview.unavailable",
          );
        }
      } finally {
        if (epoch === sequence.current) {
          pending.current = false;
          setBusy(false);
        }
      }
    },
    [projectId, reviewId],
  );

  useEffect(() => {
    const withdraw = () => {
      active.current?.abort();
      sequence.current++;
      pending.current = false;
      setVisible(null);
      setBusy(false);
      setAuthGeneration((value) => value + 1);
    };
    const storage = (event: StorageEvent) => {
      if (
        event.key === null ||
        event.key === "auth_token" ||
        event.key === "auth_refresh_pair"
      )
        withdraw();
    };
    window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, withdraw);
    window.addEventListener("storage", storage);
    return () => {
      window.removeEventListener(AUTH_TOKEN_CHANGED_EVENT, withdraw);
      window.removeEventListener("storage", storage);
    };
  }, []);

  useEffect(() => {
    if (lastTarget.current !== target) {
      lastTarget.current = target;
      capturedTarget.current = null;
      setCaptureAttempted(false);
    }
    void load("restore");
    return () => {
      active.current?.abort();
      sequence.current++;
      pending.current = false;
    };
  }, [target, authGeneration, load]);

  const document =
    visible?.target === target && visible.token === getAuthToken()
      ? visible.document
      : null;
  return (
    <div className={styles.panel}>
      <Space wrap>
        <Button disabled={busy} onClick={() => void load("restore")}>
          {t("humanReview.restore")}
        </Button>
        <Button
          type="primary"
          disabled={
            busy || captureAttempted || Boolean(document) || !getAuthToken()
          }
          onClick={() => void load("capture")}
        >
          {t("humanReview.capture")}
        </Button>
        {document && <Tag>{t("humanReview.saved")}</Tag>}
      </Space>
      {error && <Alert type="error" showIcon message={t(error)} />}
      {captureAttempted && !document && !busy && (
        <Alert type="info" message={t("humanReview.recoverHint")} />
      )}
      {busy && <Spin aria-label={t("humanReview.loading")} />}
      {document ? (
        <>
          <Typography.Text type="secondary">
            {t("humanReview.version")} {document.record.revision.slice(0, 12)}
          </Typography.Text>
          <HumanReviewFrame
            key={document.record.record_sha256}
            target={{ projectId, reviewId }}
            document={document}
            token={visible!.token}
            onWithdraw={withdrawRead}
          />
        </>
      ) : (
        !busy && !error && <Empty description={t("humanReview.empty")} />
      )}
    </div>
  );
}

export default function HumanReviewPage() {
  const { projectId = "", reviewId = "" } = useParams();
  const { t } = useTranslation();
  return (
    <main className={styles.page}>
      <Typography.Title level={4}>{t("humanReview.title")}</Typography.Title>
      <Typography.Paragraph type="secondary">
        {t("humanReview.subtitle")}
      </Typography.Paragraph>
      <Card>
        <HumanReviewPanel projectId={projectId} reviewId={reviewId} />
      </Card>
    </main>
  );
}
