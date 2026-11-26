package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
	"mosswork.desktop/internal/company"
)

func companyThreadTestApp(t *testing.T, handler http.HandlerFunc) (*App, *httptest.Server) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/devices/ws" {
			handler(w, r)
			return
		}
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		var hello map[string]any
		if wsjson.Read(r.Context(), conn, &hello) != nil {
			return
		}
		if err := wsjson.Write(r.Context(), conn, map[string]any{
			"type":              "ready",
			"device_id":         strings.Repeat("b", 32),
			"owner_user_id":     7,
			"protocol_version":  2,
			"heartbeat_seconds": 15,
			"expires_at":        time.Now().Add(time.Hour).Unix(),
			"credential":        "mosswork-device.test",
		}); err != nil {
			return
		}
		for {
			var frame map[string]any
			if wsjson.Read(r.Context(), conn, &frame) != nil {
				return
			}
		}
	}))
	client := company.NewClient(t.TempDir())
	if err := client.Pair(context.Background(), server.URL, "mosswork-pair.test", "test", runtime.GOOS); err != nil {
		server.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		client.Close(false)
		server.Close()
	})
	return &App{company: client, companyURL: server.URL, workspaceNonce: "nonce"}, server
}

func TestCompanyCreateThreadPostsLocalRequest(t *testing.T) {
	a, _ := companyThreadTestApp(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/company-runtime/threads" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("X-mosswork-Device-Credential") != "mosswork-device.test" {
			t.Fatal("missing device credential")
		}
		var body map[string]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if r.URL.Query().Get("request_id") != "creation-1" || body["agent_id"] != "expert" || body["execution_mode"] != "local" {
			t.Fatalf("request body = %#v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"thread_id":"thr_test","session_key":"dashboard:expert:7","execution_mode":"local"}`))
	})
	result, err := a.companyCreateThread("nonce", a.companyURL, json.RawMessage(`{"agent_id":"expert","request_id":"creation-1"}`))
	if err != nil {
		t.Fatal(err)
	}
	row := result.(map[string]any)
	if row["thread_id"] != "thr_test" || row["execution_mode"] != "local" {
		t.Fatalf("result = %#v", row)
	}
}

func TestCompanyCreateThreadTranslatesServerReason(t *testing.T) {
	a, _ := companyThreadTestApp(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"error":{"code":"FORBIDDEN","details":{"reason":"execution_mode_server"}}}`))
	})
	_, err := a.companyCreateThread("nonce", a.companyURL, json.RawMessage(`{"agent_id":"expert"}`))
	if err == nil || err.Error() != "execution_mode_server" {
		t.Fatalf("error = %v", err)
	}
	if got := runtimeErrorText(err.Error()); got != "此会话已在网页端使用，请新建会话进行本机执行" {
		t.Fatalf("translated error = %q", got)
	}
}

func TestCompanyCreateThreadTimeout(t *testing.T) {
	a, _ := companyThreadTestApp(t, func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(100 * time.Millisecond)
		w.WriteHeader(http.StatusOK)
	})
	old := companyCreateThreadTimeout
	companyCreateThreadTimeout = 10 * time.Millisecond
	t.Cleanup(func() { companyCreateThreadTimeout = old })
	_, err := a.companyCreateThread("nonce", a.companyURL, json.RawMessage(`{"agent_id":"expert"}`))
	if err == nil || err.Error() != "公司服务器响应超时，请重试" {
		t.Fatalf("error = %v", err)
	}
}

func TestCompanyCreateThreadWaitsOnDeviceAuthenticationRejection(t *testing.T) {
	a, _ := companyThreadTestApp(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":{"details":{"reason":"device_session_expired"}}}`))
	})
	_, err := a.companyCreateThread("nonce", a.companyURL, json.RawMessage(`{"agent_id":"expert"}`))
	if err == nil {
		t.Fatal("expected authentication rejection")
	}
	code, stage, retryable, _ := runtimeErrorMetadata(err)
	if code != "device_session_expired" || stage != "device_preparing" || !retryable {
		t.Fatalf("unexpected error classification: %s %s %t", code, stage, retryable)
	}
}
