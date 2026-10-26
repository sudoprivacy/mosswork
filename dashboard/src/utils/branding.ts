/** The desktop bundle and the server dashboard share code, but have distinct names. */
export const PRODUCT_NAME =
  import.meta.env.VITE_DESKTOP_BUNDLE === "1" ? "mosswork" : "moss-server";

export function productLogo(isDark: boolean): string {
  return `/${PRODUCT_NAME}_logo_${isDark ? "white" : "dark"}.png`;
}

/** Brand translation templates before interpolation; never rewrite user values. */
export function brandTranslations<T>(value: T): T {
  if (typeof value === "string") {
    return value.replace(
      /(?<![\w./:~-])(?:mosswork|moss-server)(?: 企业版| Enterprise)?(?![\w./:-]|\[)/g,
      PRODUCT_NAME,
    ) as T;
  }
  if (Array.isArray(value)) {
    return value.map(brandTranslations) as T;
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, text]) => [
        key,
        brandTranslations(text),
      ]),
    ) as T;
  }
  return value;
}
