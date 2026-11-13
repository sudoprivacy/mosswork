function ensureHtmlDocument(html: string): string {
  const content =
    html || "<!doctype html><html><head></head><body></body></html>";
  if (/<html\b/i.test(content)) {
    if (/<head\b/i.test(content)) return content;
    return content.replace(
      /<html\b[^>]*>/i,
      (match) => `${match}\n<head></head>`,
    );
  }
  if (/<head\b/i.test(content) || /<body\b/i.test(content)) {
    return `<!doctype html><html>${content}</html>`;
  }
  return `<!doctype html><html><head></head><body>${content}</body></html>`;
}

export function buildSandboxedHtmlDocument({
  html,
  baseHref,
  headScripts = [],
  bodyScripts = [],
}: {
  html: string;
  baseHref?: string;
  headScripts?: string[];
  bodyScripts?: string[];
}): string {
  let content = ensureHtmlDocument(html);
  const safeBaseHref = baseHref ? baseHref.replace(/"/g, "&quot;") : "";
  const headInjection = [
    ...headScripts,
    safeBaseHref ? `<base href="${safeBaseHref}">` : "",
  ]
    .filter(Boolean)
    .join("\n");

  if (headInjection) {
    content = content.replace(
      /<head\b[^>]*>/i,
      (match) => `${match}\n${headInjection}`,
    );
  }

  const bodyInjection = bodyScripts.filter(Boolean).join("\n");
  if (!bodyInjection) return content;
  if (/<\/body>/i.test(content))
    return content.replace(/<\/body>/i, `${bodyInjection}\n</body>`);
  return `${content}\n${bodyInjection}`;
}
