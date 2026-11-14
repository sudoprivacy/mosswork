/** Escape `</script` inside inline script content to prevent premature tag close. */
export function escapeScriptContent(value: string): string {
  return value.replace(/<\/script/gi, "<\\/script");
}

/** Wrap raw JS in a `<script>` tag with proper escaping. */
export function wrapInlineScript(js: string): string {
  return `<script>${escapeScriptContent(js)}</script>`;
}
