package main

import (
	"context"
	"encoding/json"
	"testing"
)

func TestDesktopRestartRequiresExplicitCompleteIdleStatus(t *testing.T) {
	for _, data := range []string{
		`{}`, `{"active_tasks":null}`, `not-json`,
		`{"active_tasks":[{}]}`,
		`{"active_tasks":[{"status":"running"}]}`,
		`{"active_tasks":[{"status":"preparing"}]}`,
		`{"active_tasks":[{"status":"unknown"}]}`,
		`{"active_tasks":[{"status":"waiting_approval"}]}`,
		`{"active_tasks":[{"status":"completed","sync_state":"pending"}]}`,
	} {
		if err := desktopUpdateIdle(json.RawMessage(data)); err == nil {
			t.Errorf("unsafe task snapshot admitted restart: %s", data)
		}
	}
	for _, data := range []string{
		`{"active_tasks":[]}`,
		`{"active_tasks":[{"status":"completed","sync_state":"synced"},{"status":"cancelled"},{"status":"failed"}]}`,
	} {
		if err := desktopUpdateIdle(json.RawMessage(data)); err != nil {
			t.Errorf("idle snapshot refused restart: %s: %v", data, err)
		}
	}
}

func TestAdmittedUpdateRefusesNewRuntimeActionsBeforeTouchingAccount(t *testing.T) {
	a := &App{}
	a.updateClosing.Store(true)
	if _, err := a.performRuntimeAction("", "", "start", json.RawMessage(`{}`)); err == nil {
		t.Fatal("new task entered an admitted restart")
	}
	if err := a.desktopUpdateTaskGuard(context.Background()); err != nil {
		t.Fatal("absent runtime should not require starting tools just to update")
	}
}

func TestDesktopUpdateTranslationsHaveIdenticalKeys(t *testing.T) {
	for key, text := range desktopUpdateCopy[LocaleEN] {
		if text == "" || desktopUpdateCopy[LocaleZH][key] == "" {
			t.Errorf("missing localized update text %s", key)
		}
	}
	for key := range desktopUpdateCopy[LocaleZH] {
		if desktopUpdateCopy[LocaleEN][key] == "" {
			t.Errorf("missing English update text %s", key)
		}
	}
}

func TestDesktopReleaseProbeRequiresActualMatchingRuntime(t *testing.T) {
	if !companyOnly {
		t.Skip("Company distribution probe")
	}
	t.Setenv("MOSSWORK_HOME", t.TempDir())
	t.Setenv("MOSSWORK_DESKTOP_PORTABLE_ZIP", "")
	previous, previousVersion := embeddedPortable, desktopVersion
	t.Cleanup(func() { embeddedPortable, desktopVersion = previous, previousVersion })
	desktopVersion = "0.0.8"
	embeddedPortable = embeddedCompanyFixture(t, "matching runtime", "0.0.8")
	if err := verifyDesktopUpdatePayload(); err != nil {
		t.Fatal(err)
	}
	embeddedPortable = embeddedCompanyFixture(t, "wrong runtime", "0.0.7")
	if err := verifyDesktopUpdatePayload(); err == nil {
		t.Fatal("native release probe accepted a shell/runtime version mismatch")
	}
}
