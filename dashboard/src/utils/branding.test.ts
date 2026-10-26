import { afterEach, expect, it, vi } from "vitest";
import { existsSync } from "node:fs";
import path from "node:path";
import { createInstance } from "i18next";
import zh from "../locales/zh.json";
import en from "../locales/en.json";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

it.each([
  ["0", "moss-server"],
  ["1", "mosswork"],
])(
  "brands both locales and wordmarks for desktop=%s",
  async (desktop, name) => {
    vi.stubEnv("VITE_DESKTOP_BUNDLE", desktop);
    vi.resetModules();
    const { PRODUCT_NAME, productLogo, brandTranslations } = await import(
      "./branding"
    );
    expect(PRODUCT_NAME).toBe(name);
    for (const bundle of [zh, en]) {
      expect(brandTranslations(bundle).app.productName).toBe(name);
      expect(brandTranslations(bundle).app.pageTitle).toBe(name);
      expect(brandTranslations(bundle).skills["mosswork-assistant"]).toMatch(
        new RegExp(`^${name} `),
      );
    }
    for (const dark of [false, true]) {
      expect(
        existsSync(path.join(process.cwd(), "public", productLogo(dark))),
      ).toBe(true);
    }
    const i18n = createInstance();
    await i18n.init({
      lng: "en",
      resources: {
        en: {
          translation: brandTranslations({
            notice: "mosswork — {{filename}}",
            paths:
              "~/.mosswork MOSSWORK_HOME https://github.com/sudoprivacy/mosswork",
          }),
        },
      },
    });
    expect(i18n.t("notice", { filename: "mosswork" })).toBe(
      `${name} — mosswork`,
    );
    expect(i18n.t("paths")).toBe(
      "~/.mosswork MOSSWORK_HOME https://github.com/sudoprivacy/mosswork",
    );
  },
);
