(function () {
  if (!document.body || !window._wails || typeof window._wails.invoke !== 'function') return;
  if (window.__mossworkWindowThemeInstalled) return;
  window.__mossworkWindowThemeInstalled = true;
  var last = '';
  function update() {
    // Read the rendered page, including an explicit appearance preference.
    var color = getComputedStyle(document.body).backgroundColor;
    var rgb = color.match(/^rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*1)?\s*\)$/);
    if (!rgb) {
      color = getComputedStyle(document.documentElement).backgroundColor;
      rgb = color.match(/^rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*1)?\s*\)$/);
    }
    if (!rgb) return;
    var value = rgb.slice(1, 4).join(':');
    if (value === last) return;
    last = value;
    window._wails.invoke('wails:event:emit:desktop:background:' + value);
  }
  new MutationObserver(update).observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme', 'class', 'style']
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', update);
  window.addEventListener('load', update);
  update();
})();
