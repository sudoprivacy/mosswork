/** Classify the innermost dependency package, never the checkout or file path. */
export function manualVendorChunk(id: string): string | undefined {
  const normalized = id.replace(/\\/g, "/").split(/[?#]/, 1)[0];
  const marker = "/node_modules/";
  const index = normalized.lastIndexOf(marker);
  if (index < 0) return undefined;
  const [name, scopedName] = normalized.slice(index + marker.length).split("/");
  if (!name || (name.startsWith("@") && !scopedName)) return undefined;
  const pkg = name.startsWith("@") ? `${name}/${scopedName}` : name;

  if (["react", "react-dom", "react-router", "react-router-dom"].includes(pkg))
    return "vendor-react";
  // Keep rc-* and @ant-design with antd: separating them creates circular
  // chunks and can cause "Cannot access 'FastColor' before initialization".
  if (
    pkg.startsWith("rc-") ||
    pkg === "antd" ||
    pkg === "antd-style" ||
    pkg.startsWith("@ant-design/")
  )
    return "vendor-antd";
  if (pkg.startsWith("@xterm/")) return "vendor-xterm";
  if (
    pkg === "react-markdown" ||
    pkg.startsWith("remark-") ||
    pkg.startsWith("rehype-") ||
    pkg === "katex"
  )
    return "vendor-markdown";
  if (pkg === "lucide-react") return "vendor-icons";
  if (
    pkg === "i18next" ||
    pkg.startsWith("i18next-") ||
    pkg === "react-i18next" ||
    pkg === "ahooks" ||
    pkg === "jszip"
  )
    return "vendor-utils";
  // Leave mermaid/graph engines and syntax highlighters to automatic async
  // chunking, along with dependencies that have no explicit vendor group.
  return undefined;
}
