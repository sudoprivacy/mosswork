package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"mosswork.desktop/internal/companyruntime"
)

func TestRuntimeIPCRejectsStaleAndUnsupportedRequests(t *testing.T) {
	valid := "desktop:runtime:session:2:start:" + url.QueryEscape(`{"thread_id":"t","text":"你好: test"}`)
	id, method, params, ok := parseRuntimeEvent(valid, "session")
	if !ok || id != "2" || method != "start" || !json.Valid(params) {
		t.Fatal("valid request rejected")
	}
	for _, bad := range []string{strings.Replace(valid, "session", "old", 1), "desktop:runtime:session:2:initialize:%7B%7D", "desktop:runtime:session:2:execute:%7B%7D", "desktop:runtime:session:2:start:[]", "desktop:runtime:session:abc:status:%7B%7D"} {
		if _, _, _, ok := parseRuntimeEvent(bad, "session"); ok {
			t.Fatalf("accepted %s", bad)
		}
	}
	if _, _, _, ok := parseRuntimeEvent(valid, ""); ok {
		t.Fatal("uninitialized nonce accepted")
	}
	for _, action := range []string{"steer", "steer_remove"} {
		request := "desktop:runtime:session:3:" + action + ":" + url.QueryEscape(`{"thread_id":"t","expected_turn_id":"turn"}`)
		if _, got, _, ok := parseRuntimeEvent(request, "session"); !ok || got != action {
			t.Fatalf("steer request rejected: %s", action)
		}
		if _, _, _, ok := parseRuntimeEvent(request, "old-session"); ok {
			t.Fatal("steer request accepted after account change")
		}
	}
	request := "desktop:runtime:session:4:create_thread:" + url.QueryEscape(`{"agent_id":"expert"}`)
	if _, method, _, ok := parseRuntimeEvent(request, "session"); !ok || method != "create_thread" {
		t.Fatal("create_thread request rejected")
	}
}

func TestRuntimeModelFailureMessages(t *testing.T) {
	if got := runtimeErrorText("company_model_failed"); got != "公司模型调用失败，请稍后重试；若持续出现请联系管理员检查模型服务。" {
		t.Fatalf("unexpected model failure message: %s", got)
	}
	for _, code := range []string{
		"company_model_timeout",
		"company_model_connection_lost",
		"company_model_http_503",
		"company_model_unknown",
	} {
		if got := runtimeErrorText(code); strings.Contains(got, "检查公司连接和客户端工具包") {
			t.Fatalf("model failure used generic connection message for %s: %s", code, got)
		}
	}
}

func TestRuntimeErrorTranslationPreservesCode(t *testing.T) {
	m := applyRuntimeErrorTranslation(companyruntime.Message{
		Error: "company_model_http_400",
	})
	if m.ErrorCode != "company_model_http_400" {
		t.Fatalf("error code was not preserved: %q", m.ErrorCode)
	}
	if m.Error == "company_model_http_400" || m.Error == "" {
		t.Fatalf("error was not translated: %q", m.Error)
	}
}

func TestRuntimeDeviceErrorsHaveActionableClassification(t *testing.T) {
	if got := runtimeErrorCode("device_session_expired"); got != "device_session_expired" {
		t.Fatalf("unexpected device code: %s", got)
	}
	if got := runtimeErrorStage("device_reconnecting_timeout"); got != "device_connection" {
		t.Fatalf("unexpected device stage: %s", got)
	}
	if !runtimeErrorRetryable("device_reconnecting_timeout") {
		t.Fatal("reconnect timeout should be retryable")
	}
	if got := runtimeErrorAction("workspace_file_locked"); got != "close_file_and_retry" {
		t.Fatalf("unexpected locked-file action: %s", got)
	}
}

func TestRuntimeRepliesPreserveKnownErrors(t *testing.T) {
	for _, code := range []string{"runtime_launch_failed", "runtime_process_disconnected", "company_protocol_incompatible", "company_runtime_disabled", "thread_already_running"} {
		if got := runtimeErrorCode(code); got != code {
			t.Errorf("runtimeErrorCode(%q) = %q", code, got)
		}
	}
	if code := runtimeErrorCode("private exception details"); code != "local_runtime_error" {
		t.Errorf("unrecognized error was exposed: %q", code)
	}
}

func TestRuntimeConflictErrorsPreserveServerReason(t *testing.T) {
	cases := []struct {
		code   string
		action string
	}{
		{code: "thread_already_running", action: "check_task_status"},
		{code: "task_identity_conflict", action: "check_task_status"},
		{code: "execution_mode_server"},
		{code: "edit_history_changed"},
		{code: "project_on_another_device"},
		{code: "previous_device_still_active", action: "check_task_status"},
	}
	for _, tc := range cases {
		t.Run(tc.code, func(t *testing.T) {
			code, stage, retryable, action := runtimeErrorMetadata(errors.New(tc.code))
			if code != tc.code {
				t.Fatalf("error code = %q, want %q", code, tc.code)
			}
			if stage != "task_reconcile" || retryable || action != tc.action {
				t.Fatalf("metadata = (%q, %t, %q), want (task_reconcile, false, %q)", stage, retryable, action, tc.action)
			}
			translated := applyRuntimeErrorTranslation(companyruntime.Message{Error: tc.code})
			if translated.ErrorCode != tc.code || translated.Error == tc.code {
				t.Fatalf("translated error = (%q, %q)", translated.ErrorCode, translated.Error)
			}
		})
	}

	code, stage, retryable, action := runtimeErrorMetadata(errors.New("unrecognized_server_reason"))
	if code != "local_runtime_error" || stage != "runtime" || retryable || action != "check_task_status" {
		t.Fatalf("unexpected generic fallback = (%q, %q, %t, %q)", code, stage, retryable, action)
	}
}

func TestRuntimePreparationErrorsAreSafelyRetryable(t *testing.T) {
	code, stage, retryable, _ := runtimeErrorMetadata(&runtimePreparationError{cause: errors.New("temporary startup failure")})
	if code != "local_runtime_error" || stage != "runtime_preparing" || !retryable {
		t.Fatalf("unexpected preparation metadata: code=%q stage=%q retryable=%t", code, stage, retryable)
	}

	_, stage, retryable, _ = runtimeErrorMetadata(errors.New("temporary startup failure"))
	if stage == "runtime_preparing" || retryable {
		t.Fatalf("unmarked runtime error became retryable: stage=%q retryable=%t", stage, retryable)
	}
}

func TestCompanyRuntimeAccountDirectoriesAreIsolated(t *testing.T) {
	t.Setenv("MOSSWORK_HOME", t.TempDir())
	a := companyStateDir("https://one", 1)
	if a == companyStateDir("https://one", 2) || a == companyStateDir("https://two", 1) || strings.Contains(a, filepath.Join(".mosswork", "portable")) {
		t.Fatal("runtime state crossed account/company scope")
	}
}
func TestOldFolderPermissionDoesNotGrantTerminalExecution(t *testing.T) {
	t.Setenv("MOSSWORK_HOME", t.TempDir())
	name := workspaceSettingsPath("https://one", 1)
	_ = os.MkdirAll(filepath.Dir(name), 0700)
	_ = os.WriteFile(name, []byte(`{"path":"/old"}`), 0600)
	if readWorkspace("https://one", 1) != "/old" || workspaceRuntimeAuthorized("https://one", 1) {
		t.Fatal("legacy folder consent was expanded")
	}
	if err := saveWorkspace("https://one", 1, "/new"); err != nil {
		t.Fatal(err)
	}
	if !workspaceRuntimeAuthorized("https://one", 1) {
		t.Fatal("explicit new authorization not saved")
	}
}
func TestCompanyPayloadFailsClosedWithoutManifestOrWithArchiveLinks(t *testing.T) {
	if validateCompanyRuntime(t.TempDir()) == nil {
		t.Fatal("missing manifest accepted")
	}
	name := filepath.Join(t.TempDir(), "payload.zip")
	out, _ := os.Create(name)
	z := zip.NewWriter(out)
	header := &zip.FileHeader{Name: "root/runtime/python"}
	header.SetMode(os.ModeSymlink | 0755)
	f, _ := z.CreateHeader(header)
	_, _ = f.Write([]byte("/outside"))
	_ = z.Close()
	_ = out.Close()
	if verifyCompanyZip(name) == nil {
		t.Fatal("archive symlink accepted")
	}
}

func TestHistoryArtifactRequiresAuthorizedDirectoryAndMatchingBytes(t *testing.T) {
	root := t.TempDir()
	file := filepath.Join(root, "result.txt")
	_ = os.WriteFile(file, []byte("result"), 0600)
	hash := sha256.Sum256([]byte("result"))
	digest := hex.EncodeToString(hash[:])
	if _, err := checkedRuntimeArtifact(root, file, digest, false); err != nil {
		t.Fatal(err)
	}
	if _, err := checkedRuntimeArtifact(root, file, "", false); err == nil {
		t.Fatal("unverified history file accepted")
	}
	outside := filepath.Join(t.TempDir(), "private.txt")
	_ = os.WriteFile(outside, []byte("result"), 0600)
	if _, err := checkedRuntimeArtifact(root, outside, digest, false); err == nil {
		t.Fatal("file outside current account grant accepted")
	}
	_ = os.WriteFile(file, []byte("changed"), 0600)
	if _, err := checkedRuntimeArtifact(root, file, digest, false); err == nil {
		t.Fatal("changed history artifact accepted")
	}
}
