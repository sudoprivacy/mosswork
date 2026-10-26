package main

import "runtime"

func mainWindowFrameless(platform string) bool {
	return platform != "windows" && platform != "darwin"
}

func (a *App) installNativeWindowChrome() {
	if a.window == nil || mainWindowFrameless(runtime.GOOS) {
		return
	}
	a.window.ExecJS(`(function(){
		if (!document.documentElement || !window._wails) return;
		if (document.documentElement.dataset.mossworkNativeTitlebar === '1') return;
		window._wails.flags = window._wails.flags || {};
		window._wails.flags.mossworkNativeTitlebar = true;
		document.documentElement.dataset.mossworkNativeTitlebar = '1';
		document.documentElement.dataset.chrome = 'native';
		document.documentElement.style.setProperty('--window-controls-inset-end', '0px');
		window.dispatchEvent(new Event('mosswork:native-titlebar-ready'));
	})();`)
}
