import { useCallback, useState } from "react";
import { Download, Eye, FolderOpen } from "lucide-react";
import { fileTreeIcon } from "@/utils/fileTreeIcon";
import { useTranslation } from "react-i18next";
import { downloadAuthFile } from "../../../components/AuthFileDownloadLink";
import {
  isDataUrl,
  needsAuthBlobFetch,
  workspacePathFromAccessUrl,
} from "../../../utils/toolMediaBlocks";
import { getDocKind } from "../../Agent/Workspace/utils/docKind";
import { getMediaKind } from "../../Agent/Workspace/utils/mediaKind";
import { isProbablyText } from "../../Agent/Workspace/utils/fileKind";
import { useChatFilePreview } from "../ChatFilePreviewContext";
import styles from "../index.module.less";

import { message as antMessage } from "@/utils/antdMessage";
import {
  requireDesktopRuntime,
  type LocalArtifact,
} from "../../../api/desktopRuntime";

/** Files the browser can render inline (vs. pure binary blobs). */
function isPreviewable(name: string): boolean {
  return (
    isProbablyText(name) ||
    getMediaKind(name) !== null ||
    getDocKind(name) !== null
  );
}

/** Authenticated download card for chat / tool-result non-image files. */
export function MessageFileCard({
  url,
  filename,
  agentId,
  workspacePath,
  localFile,
}: {
  url: string;
  filename?: string;
  agentId?: string | null;
  workspacePath?: string;
  localFile?: LocalArtifact;
}) {
  const { t } = useTranslation();
  const filePreview = useChatFilePreview();
  const [loading, setLoading] = useState(false);
  const needsAuth = needsAuthBlobFetch(url) || isDataUrl(url);
  const label = filename || url;
  const displayName = label.split(/[\\/]/).pop() || label;
  const extension = displayName.includes(".")
    ? displayName.split(".").pop()?.toUpperCase()
    : "";
  const size = localFile
    ? localFile.size < 1024
      ? `${localFile.size} B`
      : localFile.size < 1024 * 1024
      ? `${(localFile.size / 1024).toFixed(1)} KB`
      : `${(localFile.size / (1024 * 1024)).toFixed(1)} MB`
    : "";
  const canDownload = !localFile || Boolean(window.__mossworkRuntime);

  const resolvedPath = workspacePath || workspacePathFromAccessUrl(url) || "";
  const previewable = Boolean(
    !localFile &&
      resolvedPath &&
      agentId &&
      isPreviewable(label) &&
      filePreview,
  );

  const handleDownload = useCallback(async () => {
    if (localFile) {
      try {
        await requireDesktopRuntime().reveal({
          path: localFile.path,
          sha256: localFile.sha256,
        });
      } catch (error) {
        antMessage.error(
          error instanceof Error ? error.message : "无法打开文件位置",
        );
      }
      return;
    }
    if (!needsAuth) {
      const a = document.createElement("a");
      a.href = url;
      a.download = filename || "download";
      a.target = "_blank";
      a.rel = "noreferrer";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      return;
    }
    setLoading(true);
    try {
      await downloadAuthFile(url, { filename });
    } catch {
      antMessage.error(t("chat.downloadFailed", "下载失败，请重试"));
    } finally {
      setLoading(false);
    }
  }, [url, filename, needsAuth, t, localFile]);

  const openPreview = useCallback(() => {
    if (previewable && resolvedPath && filePreview) {
      filePreview.openFilePreview(resolvedPath, agentId);
    }
  }, [previewable, resolvedPath, filePreview, agentId]);

  return (
    <div className={styles.messageFileCard}>
      <div className={styles.messageFileMeta}>
        <span className={styles.messageFileIcon}>
          {fileTreeIcon(displayName, 22)}
        </span>
        <span className={styles.messageFileDetails}>
          <span className={styles.messageFileName} title={label}>
            {displayName}
          </span>
          <span className={styles.messageFileDescription}>
            {[extension || t("chat.file"), size].filter(Boolean).join(" · ")}
          </span>
          {localFile && (
            <span className={styles.messageFilePath} title={localFile.path}>
              {localFile.path}
            </span>
          )}
        </span>
      </div>
      {previewable && (
        <button
          type="button"
          className={styles.messageFileActionBtn}
          onClick={openPreview}
          title={t("common.preview")}
          aria-label={t("common.preview")}
        >
          <Eye size={15} strokeWidth={2} />
        </button>
      )}
      {canDownload && (
        <button
          type="button"
          className={styles.messageFileActionBtn}
          onClick={(e) => {
            e.stopPropagation();
            void handleDownload();
          }}
          disabled={loading}
          title={localFile ? t("chat.openFileLocation") : t("common.download")}
          aria-label={
            localFile ? t("chat.openFileLocation") : t("common.download")
          }
        >
          {localFile ? <FolderOpen size={16} /> : <Download size={16} />}
        </button>
      )}
    </div>
  );
}
