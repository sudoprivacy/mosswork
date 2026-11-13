import { useLayoutEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { getAuthToken } from "../../api/request";
import {
  humanReviewApi,
  reviewDocumentPath,
  type ReviewTarget,
  type ServedReviewDocument,
} from "../../api/modules/humanReview";
import { buildReviewFrameDocument, ReviewFrameReads } from "./frameReadBridge";
import styles from "./index.module.less";

export function HumanReviewFrame({
  target,
  document,
  token,
  onWithdraw,
}: {
  target: ReviewTarget;
  document: ServedReviewDocument;
  token: string;
  onWithdraw: () => void;
}) {
  const { t } = useTranslation();
  const { projectId, reviewId } = target;
  const frame = useRef<HTMLIFrameElement | null>(null);
  const identity = useMemo(
    () => ({ document, token, target: { projectId, reviewId } }),
    [document, token, projectId, reviewId],
  );
  const latest = useRef<typeof identity | null>(identity);
  latest.current = identity;
  const binding = useMemo(
    () => ({
      shareId: identity.target.reviewId, // SDK correlation, never a share grant.
      channel: `${crypto.randomUUID()}:${document.record.record_sha256}`,
      endpoint: `${reviewDocumentPath(identity.target)}/page`,
    }),
    [identity],
  );
  const presentation = useMemo(
    () => buildReviewFrameDocument(document, binding, window.location.origin),
    [document, binding],
  );
  useLayoutEffect(() => {
    latest.current = identity;
    const bridge = new ReviewFrameReads(
      binding,
      () => frame.current,
      () => latest.current === identity && token === getAuthToken(),
      (body, signal) =>
        humanReviewApi.readPage(identity.target, document, body, signal),
      onWithdraw,
    );
    return () => {
      latest.current = null;
      bridge.dispose();
    };
  }, [binding, identity, token, document, onWithdraw]);
  return (
    <iframe
      ref={frame}
      title={t("humanReview.frameTitle")}
      className={styles.frame}
      srcDoc={presentation}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
    />
  );
}
