import { describe, expect, it } from "vitest";
import { manualVendorChunk } from "../../vendorChunks";

const roots = [
  "/private/tmp/neutral/tree",
  "/private/tmp/m4-mosswork-src-wmW1Mt/tree",
  "/private/tmp/react-router-antd-style/@ant-design/@xterm/tree",
  "/tmp/remark-rehype-katex-mermaid-dagre-elkjs-jszip-i18next/tree",
  "C:\\checkout\\rc-probe\\react-dom\\tree",
  "C:/checkout/mermaid/lucide-react/node_modules-parent/tree",
];

describe("vendor chunks depend on package names", () => {
  it.each([
    ["react", "vendor-react"],
    ["react-dom", "vendor-react"],
    ["react-router", "vendor-react"],
    ["react-router-dom", "vendor-react"],
    ["antd", "vendor-antd"],
    ["antd-style", "vendor-antd"],
    ["rc-util", "vendor-antd"],
    ["@ant-design/colors", "vendor-antd"],
    ["@xterm/xterm", "vendor-xterm"],
    ["react-markdown", "vendor-markdown"],
    ["remark-gfm", "vendor-markdown"],
    ["rehype-katex", "vendor-markdown"],
    ["katex", "vendor-markdown"],
    ["lucide-react", "vendor-icons"],
    ["i18next", "vendor-utils"],
    ["react-i18next", "vendor-utils"],
    ["i18next-browser-languagedetector", "vendor-utils"],
    ["ahooks", "vendor-utils"],
    ["jszip", "vendor-utils"],
    ["mermaid", undefined],
    ["@dagrejs/dagre", undefined],
    ["elkjs", undefined],
    ["react-syntax-highlighter", undefined],
    ["refractor", undefined],
    ["highlight.js", undefined],
    ["unrelated-library", undefined],
  ])("keeps %s invariant across checkout paths", (pkg, expected) => {
    for (const root of roots) {
      const id = `${root}/node_modules/${pkg}/index.js`;
      expect(manualVendorChunk(id)).toBe(expected);
      expect(manualVendorChunk(id.replaceAll("/", "\\"))).toBe(expected);
    }
  });

  it("uses the innermost package for nested and pnpm dependencies", () => {
    expect(
      manualVendorChunk(
        "/repo/node_modules/antd/node_modules/jszip/lib/index.js",
      ),
    ).toBe("vendor-utils");
    expect(
      manualVendorChunk(
        "/repo/node_modules/.pnpm/rc-util@5/node_modules/rc-util/es/index.js",
      ),
    ).toBe("vendor-antd");
    expect(
      manualVendorChunk(
        "/repo/node_modules/@ant-design/colors/node_modules/unrelated/index.js",
      ),
    ).toBeUndefined();
  });

  it("does not match a filename, query, similar package or application source", () => {
    for (const id of [
      "/repo/src/rc-panel/node_modules-helper.js",
      "/repo/node_modules-other/rc-util/index.js",
      "/repo/node_modules/unrelated/rc-helper/antd/index.js",
      "/repo/node_modules/unrelated/index.js?target=/node_modules/react/index.js",
      "/repo/node_modules/not-jszip/index.js",
      "/repo/node_modules/antd-other/index.js",
      "\0commonjsHelpers.js",
    ])
      expect(manualVendorChunk(id)).toBeUndefined();
    expect(
      manualVendorChunk("\0/repo/node_modules/react/index.js?commonjs-proxy"),
    ).toBe("vendor-react");
  });
});
