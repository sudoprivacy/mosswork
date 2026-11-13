import { useEffect, useRef, useState, type ReactNode } from "react";
import { requestBlob } from "../api/request";

type Props = {
  src?: string | null;
  fallback: ReactNode;
  size?: number;
  alt?: string;
};

/** Load protected market artwork with the same authenticated request as the rest of the dashboard. */
export default function MarketIcon({
  src,
  fallback,
  size = 38,
  alt = "",
}: Props) {
  const hostRef = useRef<HTMLSpanElement>(null);
  const [inView, setInView] = useState(false);
  const [objectUrl, setObjectUrl] = useState<string | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !src) {
      setInView(false);
      return;
    }
    if (!("IntersectionObserver" in window)) {
      setInView(true);
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setInView(true);
          observer.disconnect();
        }
      },
      { rootMargin: "240px" },
    );
    observer.observe(host);
    return () => observer.disconnect();
  }, [src]);

  useEffect(() => {
    let cancelled = false;
    let nextUrl: string | null = null;
    setObjectUrl(null);
    if (!src || !inView) return;
    void requestBlob(src)
      .then((blob) => {
        if (cancelled) return;
        nextUrl = URL.createObjectURL(blob);
        setObjectUrl(nextUrl);
      })
      .catch(() => {
        if (!cancelled) setObjectUrl(null);
      });
    return () => {
      cancelled = true;
      if (nextUrl) URL.revokeObjectURL(nextUrl);
    };
  }, [inView, src]);

  return (
    <span
      ref={hostRef}
      aria-hidden={alt ? undefined : true}
      style={{
        width: size,
        height: size,
        flex: `0 0 ${size}px`,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        overflow: "hidden",
        borderRadius: 10,
        background: objectUrl ? "transparent" : "var(--ant-color-fill-quaternary, #f5f5f5)",
      }}
    >
      {objectUrl ? (
        <img
          src={objectUrl}
          alt={alt}
          loading="lazy"
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      ) : (
        fallback
      )}
    </span>
  );
}
