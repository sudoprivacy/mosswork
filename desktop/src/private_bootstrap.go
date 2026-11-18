package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"mosswork.desktop/internal/privatebootstrap"
)

type privateBootstrapStatus struct {
	Available  bool `json:"available"`
	Configured bool `json:"configured"`
}

// Set by the company packager after staging the exact Stack-produced bundle.
// Empty pins disable execution; payload metadata cannot supply its own trust.
var privateManifestSHA256 string
var privateChecksumsSHA256 string

// This helper checks the actual application payload without opening the UI,
// reading an account, installing anything, or starting a Node.
func verifyPrivatePayload() error {
	_, err := privateInstaller()
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{
		"client_version": desktopVersion, "platform": runtime.GOOS,
		"arch": runtime.GOARCH, "payload_verified": true,
	})
}

func privateInstaller() (privatebootstrap.Installer, error) {
	if runtime.GOOS != "darwin" {
		return privatebootstrap.Installer{}, errors.New("Private desktop bootstrap requires macOS")
	}
	executable, err := os.Executable()
	if err != nil {
		return privatebootstrap.Installer{}, err
	}
	resolved, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return privatebootstrap.Installer{}, err
	}
	bundle := filepath.Join(filepath.Dir(resolved), "../Resources/private-bootstrap")
	bundle, err = filepath.Abs(bundle)
	if err != nil {
		return privatebootstrap.Installer{}, err
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return privatebootstrap.Installer{}, err
	}
	i := privatebootstrap.Installer{
		Bundle: bundle, Root: filepath.Join(home, ".local/share/sudoprivate/personal-macos"), Verifier: resolved,
		Platform:       map[string]string{"arm64": "macos-arm64", "amd64": "macos-x64"}[runtime.GOARCH],
		ManifestSHA256: privateManifestSHA256, ChecksumsSHA256: privateChecksumsSHA256,
	}
	return i, i.VerifyBundle(i.Platform)
}

// The main login page offers Cloud sign-in when its deployment component is
// installed. This is a capability of the company client, not another app mode.
func (a *App) GetBootstrapStatus(ctx context.Context) (privateBootstrapStatus, error) {
	if err := a.requireLoginPanel(ctx); err != nil {
		return privateBootstrapStatus{}, err
	}
	i, err := privateInstaller()
	if err != nil {
		return privateBootstrapStatus{}, nil
	}
	return privateBootstrapStatus{Available: true, Configured: i.Configured()}, nil
}

func (a *App) SendCloudLoginCode(ctx context.Context, phone string) (int, error) {
	if err := a.requireLoginPanel(ctx); err != nil {
		return 0, err
	}
	if _, err := privateInstaller(); err != nil {
		return 0, errors.New("This installer does not contain the Private environment")
	}
	if !a.loginBusy.CompareAndSwap(false, true) {
		return 0, errors.New("Login is already in progress")
	}
	defer a.loginBusy.Store(false)
	client := privatebootstrap.NewClient()
	available, err := client.PhoneLoginAvailable(ctx)
	if err != nil {
		return 0, err
	}
	if !available {
		return 0, errors.New("Phone login is not currently available")
	}
	return client.SendCode(ctx, phone)
}

func (a *App) LoginCloud(ctx context.Context, phone, code string) error {
	if err := a.requireLoginPanel(ctx); err != nil {
		return err
	}
	if !a.loginBusy.CompareAndSwap(false, true) {
		return errors.New("Login is already in progress")
	}
	defer a.loginBusy.Store(false)
	i, err := privateInstaller()
	if err != nil {
		return errors.New("This installer does not contain the Private environment")
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	if _, err = privatebootstrap.NewClient().LoginAndConfigure(ctx, phone, code, i.Configure); err != nil {
		return err
	}
	if err = i.Prepare(ctx); err != nil {
		return err
	}
	return i.Open()
}

func (a *App) OpenBootstrapAssistant(ctx context.Context) error {
	if err := a.requireLoginPanel(ctx); err != nil {
		return err
	}
	if !a.loginBusy.CompareAndSwap(false, true) {
		return errors.New("Login is already in progress")
	}
	defer a.loginBusy.Store(false)
	i, err := privateInstaller()
	if err != nil {
		return err
	}
	return i.Open()
}
