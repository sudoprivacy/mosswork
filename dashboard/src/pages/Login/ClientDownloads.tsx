import { useEffect, useState } from "react";
import { Download, LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  rootApi,
  type ClientDownloads as Downloads,
} from "../../api/modules/root";
import styles from "./ClientDownloads.module.less";

export default function ClientDownloads() {
  const { t } = useTranslation();
  const [downloads, setDownloads] = useState<Downloads | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    rootApi
      .getClientDownloads()
      .then((result) => {
        if (!cancelled) setDownloads(result);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!downloads) {
    return (
      <p className={styles.status} role="status">
        {!failed && <LoaderCircle size={16} aria-hidden="true" />}
        {t(
          failed
            ? "login.clientDownloads.unavailable"
            : "login.clientDownloads.loading",
        )}
      </p>
    );
  }

  return (
    <section
      className={styles.downloads}
      aria-labelledby="client-downloads-title"
    >
      <h3 id="client-downloads-title" className={styles.title}>
        {t("login.clientDownloads.title")}
      </h3>
      <p className={styles.version}>
        {t("login.clientDownloads.version", { version: downloads.version })}
      </p>
      <div className={styles.links}>
        <a
          href={downloads.windows_url}
          target="_blank"
          rel="noopener noreferrer"
          className={styles.link}
        >
          <Download size={16} aria-hidden="true" />
          <span>
            Windows<small>x64</small>
          </span>
        </a>
        <a
          href={downloads.macos_url}
          target="_blank"
          rel="noopener noreferrer"
          className={styles.link}
        >
          <Download size={16} aria-hidden="true" />
          <span>
            macOS<small>Apple Silicon · 13+</small>
          </span>
        </a>
      </div>
    </section>
  );
}
