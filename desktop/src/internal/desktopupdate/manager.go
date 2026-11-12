package desktopupdate

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"

	"github.com/wailsapp/wails/v3/pkg/updater"
)

// Status is the sole update state consumed by every native window.
type Status struct {
	State          updater.State `json:"state"`
	CurrentVersion string        `json:"current_version"`
	LatestVersion  string        `json:"latest_version,omitempty"`
	Written        int64         `json:"written"`
	Total          int64         `json:"total"`
	ErrorStage     string        `json:"error_stage,omitempty"`
	Error          string        `json:"error,omitempty"`
	IsReady        bool          `json:"is_ready"`
}

// Manager serializes discovery, staging and restart around the framework's
// state machine. It never installs an update merely because one was found.
type Manager struct {
	operation sync.Mutex
	mu        sync.Mutex
	engine    *updater.Updater
	status    Status
	quit      func()
	changed   func()
}

func New(version string, provider updater.Provider, quit, changed func()) (*Manager, error) {
	m := &Manager{quit: quit, changed: changed}
	m.engine = updater.New(m)
	if err := m.engine.Init(updater.Config{CurrentVersion: version,
		Providers: []updater.Provider{provider}, Window: updater.WindowNone}); err != nil {
		return nil, err
	}
	return m, nil
}

func (m *Manager) Status() Status {
	m.mu.Lock()
	defer m.mu.Unlock()
	status := m.status
	status.State = m.engine.State()
	status.CurrentVersion = m.engine.CurrentVersion()
	status.IsReady = status.State == updater.StateReady
	return status
}

func (m *Manager) Check(ctx context.Context) error {
	if !m.operation.TryLock() {
		return errors.New("desktop update operation already in progress")
	}
	defer m.operation.Unlock()
	// Keep the verified candidate when a second window requests a check.
	if m.engine.State() == updater.StateReady {
		return nil
	}
	m.clearError()
	m.mu.Lock()
	m.status.LatestVersion, m.status.Written, m.status.Total = "", 0, 0
	m.mu.Unlock()
	_, err := m.engine.Check(ctx)
	return err
}

func (m *Manager) Download(ctx context.Context) error {
	if !m.operation.TryLock() {
		return errors.New("desktop update operation already in progress")
	}
	defer m.operation.Unlock()
	if m.engine.State() == updater.StateReady {
		return nil
	}
	m.clearError()
	return m.engine.DownloadAndInstall(ctx)
}

// Restart calls the application's task-admission guard immediately before the
// framework spawns its helper. The caller must hold that guard until this
// method returns, keeping new task submissions out of the check/quit gap.
func (m *Manager) Restart(ctx context.Context, guard func() error) error {
	m.operation.Lock()
	defer m.operation.Unlock()
	if m.engine.State() != updater.StateReady {
		return updater.ErrNotReady
	}
	m.clearError()
	if err := guard(); err != nil {
		m.fail("busy", err)
		return err
	}
	executable, err := os.Executable()
	if err == nil {
		err = writableTarget(Target(executable, runtime.GOOS))
	}
	if err != nil {
		m.fail("location", err)
		return err
	}
	if err = m.engine.Restart(ctx); err != nil {
		m.fail("install", err)
	}
	return err
}

// Close removes only this manager's uncommitted staging directory. Cancel
// operations first, and skip Close after a restart helper accepted ownership.
func (m *Manager) Close() {
	m.operation.Lock()
	defer m.operation.Unlock()
	staged := m.engine.DownloadedPath()
	if staged != "" && strings.HasPrefix(filepath.Base(filepath.Dir(staged)), "wails-update-") {
		_ = os.RemoveAll(filepath.Dir(staged))
	}
}

func writableTarget(target string) error {
	probe, err := os.CreateTemp(filepath.Dir(target), ".mosswork-update-write-*")
	if err != nil {
		return err
	}
	name := probe.Name()
	if err = probe.Close(); err != nil {
		_ = os.Remove(name)
		return err
	}
	return os.Remove(name)
}

func (m *Manager) clearError() {
	m.mu.Lock()
	m.status.Error, m.status.ErrorStage = "", ""
	m.mu.Unlock()
}

func (m *Manager) fail(stage string, err error) {
	m.mu.Lock()
	m.status.Error, m.status.ErrorStage = err.Error(), stage
	m.mu.Unlock()
	m.notify()
}

func (m *Manager) notify() {
	if m.changed != nil {
		m.changed()
	}
}

// Host callbacks keep native UI state independent of any dashboard connection.
func (m *Manager) Emit(name string, data ...any) bool {
	defer m.notify()
	if len(data) == 0 {
		return true
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	switch payload := data[0].(type) {
	case *updater.Release:
		m.status.LatestVersion = payload.Version
	case updater.Progress:
		m.status.Written, m.status.Total = payload.Written, payload.Total
	case updater.ErrorInfo:
		m.status.ErrorStage, m.status.Error = string(payload.Stage), payload.Message
	}
	return true
}

func (m *Manager) Quit()                                                 { m.quit() }
func (m *Manager) OnEvent(string, func(any)) func()                      { return func() {} }
func (m *Manager) OpenWindow(updater.WindowOptions) updater.WindowHandle { return nil }
