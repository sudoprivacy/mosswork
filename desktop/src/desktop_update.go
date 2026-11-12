package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/wailsapp/wails/v3/pkg/application"
	"github.com/wailsapp/wails/v3/pkg/events"
	"github.com/wailsapp/wails/v3/pkg/updater"
	"mosswork.desktop/internal/desktopupdate"
)

type DesktopUpdateView struct {
	Status  desktopupdate.Status `json:"status"`
	Strings map[string]string    `json:"strings"`
	Locale  Locale               `json:"locale"`
}

func (a *App) GetDesktopUpdate() DesktopUpdateView {
	locale := a.store.get().Locale
	view := DesktopUpdateView{Status: desktopupdate.Status{State: updater.StateUnconfigured, CurrentVersion: desktopVersion}, Strings: map[string]string{}, Locale: locale}
	for key := range desktopUpdateCopy[LocaleEN] {
		text := desktopUpdateCopy[locale][key]
		if text == "" {
			text = desktopUpdateCopy[LocaleEN][key]
		}
		view.Strings[key] = text
	}
	if a.updates != nil {
		view.Status = a.updates.Status()
	}
	return view
}

func (a *App) configureDesktopUpdates() {
	// Development/base-mode binaries must not replace themselves with the
	// enterprise distribution. Release packaging stamps only complete clients.
	if !companyOnly || desktopVersion == "dev" || !desktopupdate.Supported(runtime.GOOS, runtime.GOARCH) {
		return
	}
	source, err := desktopupdate.NewSource()
	if err == nil {
		a.updates, err = desktopupdate.New(desktopVersion, source, a.requestQuit, func() {
			a.app.Event.Emit("desktop:update:changed")
		})
	}
	if err != nil {
		log.Printf("configure desktop updates: %v", err)
	}
	a.updateContext, a.updateCancel = context.WithCancel(context.Background())
	for _, action := range []string{"check", "download", "restart"} {
		name := action
		a.app.Event.On("desktop:update:"+name, func(event *application.CustomEvent) {
			a.updateMu.Lock()
			allowed := a.updateWindow != nil && event.Sender == a.updateWindow.Name()
			a.updateMu.Unlock()
			if allowed {
				go a.runDesktopUpdateAction(name)
			}
		})
	}
}

func (a *App) ShowDesktopUpdate() {
	a.updateMu.Lock()
	defer a.updateMu.Unlock()
	if a.app == nil {
		return
	}
	if a.updateWindow == nil {
		a.updateWindow = a.app.Window.NewWithOptions(application.WebviewWindowOptions{
			Name: "desktop-update", Title: a.GetDesktopUpdate().Strings["title"],
			Width: 480, Height: 400, URL: "/desktop-update.html", AllowSimpleEventEmit: true,
		})
		a.updateWindow.RegisterHook(events.Common.WindowClosing, func(event *application.WindowEvent) {
			event.Cancel()
			a.updateWindow.Hide()
		})
	}
	a.updateWindow.Show()
	a.updateWindow.Focus()
}

func (a *App) startDesktopUpdateChecks() {
	if a.updates == nil {
		return
	}
	ctx := a.updateContext
	go func() {
		ticker := time.NewTicker(6 * time.Hour)
		defer ticker.Stop()
		for {
			check, stop := context.WithTimeout(ctx, 30*time.Second)
			err := a.updates.Check(check)
			stop()
			if err == nil && a.updates.Status().State == updater.StateAvailable {
				a.ShowDesktopUpdate()
			}
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
}

func (a *App) runDesktopUpdateAction(action string) {
	if a.updates == nil {
		return
	}
	ctx, cancel := context.WithTimeout(a.updateContext, 20*time.Minute)
	defer cancel()
	var err error
	switch action {
	case "check":
		check, stop := context.WithTimeout(ctx, 30*time.Second)
		err = a.updates.Check(check)
		stop()
	case "download":
		err = a.updates.Download(ctx)
	case "restart":
		a.updateGate.Lock()
		defer a.updateGate.Unlock()
		if a.updateClosing.Load() {
			return
		}
		err = a.updates.Restart(ctx, func() error {
			if err := a.desktopUpdateTaskGuard(ctx); err != nil {
				return err
			}
			a.updateClosing.Store(true)
			return nil
		})
		if err != nil {
			a.updateClosing.Store(false)
		}
	}
	if err != nil {
		log.Printf("desktop update %s: %v", action, err)
	}
}

func (a *App) desktopUpdateTaskGuard(ctx context.Context) error {
	a.companyMu.Lock()
	process := a.companyRuntime
	a.companyMu.Unlock()
	if process == nil {
		return nil
	}
	// A dead worker may have interrupted tasks. Require normal reconciliation
	// rather than guessing that the absence of a process means idle.
	if !process.Alive() {
		return errors.New("local runtime state is unavailable; reconcile tasks before updating")
	}
	check, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	status, err := process.Request(check, "status", json.RawMessage(`{}`))
	if err != nil {
		return err
	}
	return desktopUpdateIdle(status)
}

// Release tooling invokes the actual native executable in an isolated home.
// This path must run before account loading, workers or a webview are started.
func verifyDesktopUpdatePayload() error {
	if !companyOnly || desktopVersion == "dev" {
		return errors.New("native update verification requires a versioned company client")
	}
	root, err := ensureCompanyPortable()
	if err != nil {
		return err
	}
	data, err := os.ReadFile(filepath.Join(root, "company-runtime-manifest.json"))
	if err != nil {
		return err
	}
	var manifest struct {
		Version string `json:"mosswork_version"`
	}
	if err = json.Unmarshal(data, &manifest); err != nil {
		return err
	}
	if manifest.Version != desktopVersion {
		return errors.New("native client and bundled runtime versions differ")
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]string{
		"client_version": desktopVersion, "runtime_version": manifest.Version,
		"platform": runtime.GOOS, "arch": runtime.GOARCH,
	})
}

func desktopUpdateIdle(data json.RawMessage) error {
	var snapshot struct {
		Tasks *[]struct {
			Status    string `json:"status"`
			SyncState string `json:"sync_state"`
		} `json:"active_tasks"`
	}
	if json.Unmarshal(data, &snapshot) != nil || snapshot.Tasks == nil {
		return errors.New("local task status is incomplete")
	}
	for _, task := range *snapshot.Tasks {
		if (task.Status != "completed" && task.Status != "failed" && task.Status != "cancelled") || task.SyncState == "pending" {
			return errors.New("finish or reconcile local tasks and pending results before updating")
		}
	}
	return nil
}

var desktopUpdateCopy = map[Locale]map[string]string{
	LocaleEN: {
		"title": "mosswork updates", "current": "Current version", "latest": "New version",
		"unconfigured": "Automatic updates are available in supported stable company installers.",
		"idle":         "Check for a new client version.", "checking": "Checking for updates…",
		"up-to-date": "You’re up to date.", "available": "A new version is available.",
		"downloading": "Downloading the complete client…", "verifying": "Verifying the download…",
		"installing": "Preparing the update…", "ready": "The update is ready to install.",
		"error":    "Update failed. Your current version is still available. Please try again.",
		"busy":     "Finish or reconcile local tasks and wait for results to sync before restarting.",
		"location": "Move mosswork to a writable Applications folder before updating.",
		"check":    "Check for updates", "download": "Download update", "restart": "Restart and install",
		"confirm": "Save your work before continuing. Restart mosswork and install the verified update now?",
		"note":    "Your account settings and working folders are preserved. Installation restarts the client; save any unsent edits first.",
	},
	LocaleZH: {
		"title": "mosswork 客户端更新", "current": "当前版本", "latest": "新版本",
		"unconfigured": "请使用受支持的正式版企业客户端安装包启用自动更新。",
		"idle":         "检查是否有新版客户端。", "checking": "正在检查更新…",
		"up-to-date": "当前已是最新版本。", "available": "发现新版客户端。",
		"downloading": "正在下载完整客户端…", "verifying": "正在校验下载内容…",
		"installing": "正在准备更新…", "ready": "更新已准备好，可以重启安装。",
		"error":    "更新未完成，当前版本仍可使用，请稍后重试。",
		"busy":     "请完成或核对本地任务，并等待任务结果同步后再重启安装。",
		"location": "请先将 mosswork 移到有写入权限的“应用程序”文件夹，再进行更新。",
		"check":    "检查更新", "download": "下载更新", "restart": "重启并安装",
		"confirm": "请先保存正在编辑的内容。现在重启 mosswork 并安装已校验的更新吗？",
		"note":    "更新会保留账号设置和工作目录。安装需要重启客户端，请先保存未发送的编辑内容。",
	},
}
