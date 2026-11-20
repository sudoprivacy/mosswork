package company

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

func TestBaseURLRequiresHTTPOrigin(t *testing.T) {
	for _, raw := range []string{"ftp://localhost", "https://user:pass@company.test", "https://company.test/path", "https://company.test/?token=secret", "file:///tmp", "https://company.test/#fragment"} {
		if _, err := BaseURL(raw); err == nil {
			t.Errorf("accepted %s", raw)
		}
	}
	for _, raw := range []string{"http://company.test:8088/", "http://10.20.30.40:8088/", "https://company.test:8443/", "https://mosswork.corp.internal/", "https://10.20.30.40:8443/", "https://[fd00::10]:8443/"} {
		if got, err := BaseURL(raw); err != nil || got != strings.TrimSuffix(raw, "/") {
			t.Fatal(got, err)
		}
	}
}

func TestCompanyPostUsesCurrentDeviceCredentialAndBody(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("X-mosswork-Device-Credential"); got != "mosswork-device.test" {
			t.Fatalf("credential header = %q", got)
		}
		var body map[string]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["agent_id"] != "expert" {
			t.Fatalf("request body = %#v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"thread_id":"thr_test","execution_mode":"local"}`))
	}))
	defer server.Close()

	client := NewClient(t.TempDir())
	defer client.Close(false)
	client.httpClient.Transport = server.Client().Transport
	client.status = Status{State: "online", DeviceID: "device", OwnerUserID: 7, ExpiresAt: time.Now().Add(time.Hour).Unix()}
	client.credential = "mosswork-device.test"

	status, response, err := client.CompanyPost(
		context.Background(), server.URL, "/api/company-runtime/threads",
		[]byte(`{"agent_id":"expert"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	if status != http.StatusCreated || string(response) != `{"thread_id":"thr_test","execution_mode":"local"}` {
		t.Fatalf("response = %d %s", status, response)
	}
}

func TestCompanyPostHonorsContextTimeout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(100 * time.Millisecond)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	client := NewClient(t.TempDir())
	defer client.Close(false)
	client.status = Status{State: "online", DeviceID: "device", OwnerUserID: 7, ExpiresAt: time.Now().Add(time.Hour).Unix()}
	client.credential = "mosswork-device.test"
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if _, _, err := client.CompanyPost(ctx, server.URL, "/api/company-runtime/threads", []byte(`{}`)); err == nil {
		t.Fatal("expected context timeout")
	}
}

func testServer(t *testing.T, handler func(context.Context, *websocket.Conn, hello, ready)) (*Client, string) {
	t.Helper()
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/devices/ws" || r.URL.RawQuery != "" || r.Header.Get("Origin") != "" {
			t.Error("unexpected request")
			return
		}
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.CloseNow()
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		var h hello
		if err := wsjson.Read(ctx, conn, &h); err != nil {
			t.Error(err)
			return
		}
		identity := ready{"ready", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 7, ProtocolVersion, 15, time.Now().Add(time.Hour).Unix(), "mosswork-device.test-credential-for-channel-only", false}
		if err := wsjson.Write(ctx, conn, identity); err != nil {
			t.Error(err)
			return
		}
		handler(ctx, conn, h, identity)
	}))
	client := NewClient(t.TempDir())
	client.httpClient.Transport = server.Client().Transport // Trust only this test server's CA.
	t.Cleanup(func() { client.Close(false); server.Close() })
	return client, server.URL
}

func testCall(identity ready) frame {
	return frame{Type: "call", CallID: "call-1", TaskID: "probe:call-1", DeviceID: identity.DeviceID,
		OwnerUserID: identity.OwnerUserID, Tool: "device_status", ExpiresAt: time.Now().Add(30 * time.Second).Unix(), Arguments: map[string]json.RawMessage{}}
}

func TestPairAndProbeBothPlatforms(t *testing.T) {
	for _, platform := range []string{"windows", "darwin"} {
		t.Run(platform, func(t *testing.T) {
			results := make(chan map[string]any, 3)
			client, base := testServer(t, func(ctx context.Context, conn *websocket.Conn, h hello, identity ready) {
				if h.Type != "pair" || h.Platform != platform || h.Credential != "mosswork-pair.test" || len(h.Capabilities) != 2 || h.Capabilities[0] != "device_status" {
					t.Error("unexpected hello")
				}
				call := testCall(identity)
				for range 2 { // replayed call gets the same result
					if err := wsjson.Write(ctx, conn, call); err != nil {
						t.Error(err)
						return
					}
					var result map[string]any
					if err := wsjson.Read(ctx, conn, &result); err != nil {
						t.Error(err)
						return
					}
					results <- result
				}
				if err := wsjson.Write(ctx, conn, map[string]string{"type": "cancel", "call_id": call.CallID}); err != nil {
					t.Error(err)
					return
				}
				var result map[string]any
				if err := wsjson.Read(ctx, conn, &result); err != nil {
					t.Error(err)
					return
				}
				results <- result // already completed must not be relabelled cancelled
				var ignored map[string]any
				_ = wsjson.Read(ctx, conn, &ignored)
			})
			if err := client.Pair(context.Background(), base, "mosswork-pair.test", "My computer", platform); err != nil {
				t.Fatal(err)
			}
			encoded, _ := json.Marshal(client.Status())
			if strings.Contains(string(encoded), "credential") || client.Status().OwnerUserID != 7 {
				t.Fatal(string(encoded))
			}
			for range 3 {
				select {
				case result := <-results:
					if result["type"] != "result" || result["call_id"] != "call-1" || result["result"].(map[string]any)["platform"] != platform {
						t.Fatal(result)
					}
				case <-time.After(3 * time.Second):
					t.Fatal("probe did not return")
				}
			}
		})
	}
}

func TestRejectCrossDeviceOwnerArbitraryToolAndExpiredCalls(t *testing.T) {
	for _, mutation := range []string{"device", "owner", "tool", "expiry", "arguments", "task"} {
		t.Run(mutation, func(t *testing.T) {
			closed := make(chan websocket.StatusCode, 1)
			client, base := testServer(t, func(ctx context.Context, conn *websocket.Conn, _ hello, identity ready) {
				call := testCall(identity)
				switch mutation {
				case "device":
					call.DeviceID = "someone-else"
				case "owner":
					call.OwnerUserID++
				case "tool":
					call.Tool = "shell"
				case "expiry":
					call.ExpiresAt = 1
				case "arguments":
					call.Arguments["command"] = json.RawMessage(`"whoami"`)
				case "task":
					call.TaskID = "forged"
				}
				_ = wsjson.Write(ctx, conn, call)
				var result any
				closed <- websocket.CloseStatus(wsjson.Read(ctx, conn, &result))
			})
			if err := client.Pair(context.Background(), base, "mosswork-pair.test", "PC", "windows"); err != nil {
				t.Fatal(err)
			}
			select {
			case code := <-closed:
				if code != 4002 {
					t.Fatal(code)
				}
			case <-time.After(3 * time.Second):
				t.Fatal("invalid call accepted")
			}
		})
	}
}

func TestUntrustedCertificateFailsAndLeavesNoCredential(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Error("untrusted server reached") }))
	defer server.Close()
	client := NewClient(t.TempDir())
	if err := client.Pair(context.Background(), server.URL, "mosswork-pair.test", "PC", "darwin"); err == nil {
		t.Fatal("accepted untrusted certificate")
	}
	if client.Status().State != "unpaired" || client.Status().DeviceID != "" {
		t.Fatal(client.Status())
	}
}

func TestHTTPPairAndProbe(t *testing.T) {
	results := make(chan frame, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.CloseNow()
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		var h hello
		if err := wsjson.Read(ctx, conn, &h); err != nil {
			t.Error(err)
			return
		}
		identity := ready{"ready", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 7, ProtocolVersion, 15, time.Now().Add(time.Hour).Unix(), "mosswork-device.test-credential-for-channel-only", false}
		if err := wsjson.Write(ctx, conn, identity); err != nil {
			t.Error(err)
			return
		}
		if err := wsjson.Write(ctx, conn, testCall(identity)); err != nil {
			t.Error(err)
			return
		}
		var result frame
		if err := wsjson.Read(ctx, conn, &result); err != nil {
			t.Error(err)
			return
		}
		results <- result
		var ignored any
		_ = wsjson.Read(ctx, conn, &ignored)
	}))
	defer server.Close()
	client := NewClient(t.TempDir())
	defer client.Close(false)
	if err := client.Pair(context.Background(), server.URL, "mosswork-pair.test", "PC", "windows"); err != nil {
		t.Fatal(err)
	}
	select {
	case result := <-results:
		if result.Type != "result" || result.CallID != "call-1" {
			t.Fatal(result)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("plain WebSocket probe did not complete")
	}
}

func TestDisconnectRequestsOwnRevocation(t *testing.T) {
	revoked := make(chan string, 1)
	client, base := testServer(t, func(ctx context.Context, conn *websocket.Conn, _ hello, _ ready) {
		var f map[string]string
		if err := wsjson.Read(ctx, conn, &f); err != nil {
			t.Error(err)
			return
		}
		revoked <- f["type"]
	})
	if err := client.Pair(context.Background(), base, "mosswork-pair.test", "PC", "windows"); err != nil {
		t.Fatal(err)
	}
	client.Close(true)
	select {
	case kind := <-revoked:
		if kind != "revoke" {
			t.Fatal(kind)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("missing revoke")
	}
}

func TestReconnectUsesDeviceCredentialWithoutRePairing(t *testing.T) {
	var connections atomic.Int32
	reconnected := make(chan hello, 1)
	expires := time.Now().Add(time.Hour).Unix()
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.CloseNow()
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		var h hello
		if err := wsjson.Read(ctx, conn, &h); err != nil {
			return
		}
		identity := ready{"ready", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 7, ProtocolVersion, 15, expires, "mosswork-device.test-credential-for-channel-only", false}
		if err := wsjson.Write(ctx, conn, identity); err != nil {
			t.Error(err)
			return
		}
		if connections.Add(1) == 1 {
			if h.Type != "pair" {
				t.Error("initial handshake did not pair")
			}
			_ = conn.Close(1012, "server_stopping")
			return
		}
		reconnected <- h
		var ignored any
		_ = wsjson.Read(ctx, conn, &ignored)
	}))
	defer server.Close()
	client := NewClient(t.TempDir())
	client.httpClient.Transport = server.Client().Transport
	defer client.Close(false)
	if err := client.Pair(context.Background(), server.URL, "mosswork-pair.test", "PC", "darwin"); err != nil {
		t.Fatal(err)
	}
	select {
	case h := <-reconnected:
		if h.Type != "hello" || !strings.HasPrefix(h.Credential, "mosswork-device.") {
			t.Fatal("reconnection reused pairing code")
		}
	case <-time.After(4 * time.Second):
		t.Fatal("did not reconnect")
	}
}

func TestRuntimeIdentityWaitsForTransientReconnect(t *testing.T) {
	client := NewClient(t.TempDir())
	client.mu.Lock()
	client.status = Status{State: "reconnecting", DeviceID: "device", OwnerUserID: 7, ExpiresAt: time.Now().Add(time.Hour).Unix()}
	client.credential = "mosswork-device.test"
	client.mu.Unlock()
	go func() {
		time.Sleep(40 * time.Millisecond)
		client.mu.Lock()
		client.status.State = "online"
		client.mu.Unlock()
	}()

	status, credential, err := client.RuntimeIdentity()
	if err != nil || status.State != "online" || credential != "mosswork-device.test" {
		t.Fatalf("identity did not wait for reconnect: %+v %q %v", status, credential, err)
	}
}

func TestRuntimeIdentityReportsExpiredDeviceCredential(t *testing.T) {
	client := NewClient(t.TempDir())
	client.mu.Lock()
	client.status = Status{State: "disconnected", ExpiresAt: time.Now().Add(-time.Minute).Unix()}
	client.mu.Unlock()
	_, _, err := client.RuntimeIdentity()
	if err == nil || err.Error() != "device_session_expired" {
		t.Fatalf("unexpected identity error: %v", err)
	}
}

func TestNativeGrantAndFileCallOverTLS(t *testing.T) {
	replies := make(chan FileResult, 2)
	client, base := testServer(t, func(ctx context.Context, conn *websocket.Conn, h hello, identity ready) {
		var permissions struct {
			Type   string  `json:"type"`
			Grants []Grant `json:"grants"`
		}
		if err := wsjson.Read(ctx, conn, &permissions); err != nil {
			t.Error(err)
			return
		}
		if permissions.Type != "grants" || len(permissions.Grants) != 1 {
			t.Errorf("bad grants %+v", permissions)
			return
		}
		g := permissions.Grants[0]
		call := fileCall(g, "local_write_file", "report.docx", "TLS report")
		call.Type = "call"
		call.DeviceID = identity.DeviceID
		call.OwnerUserID = identity.OwnerUserID
		if err := wsjson.Write(ctx, conn, call); err != nil {
			t.Error(err)
			return
		}
		var result struct {
			Type   string     `json:"type"`
			Result FileResult `json:"result"`
		}
		if err := wsjson.Read(ctx, conn, &result); err != nil {
			t.Error(err)
			return
		}
		replies <- result.Result
		// Reconciliation is a separate call and returns the original receipt.
		query := fileCall(g, "local_operation_status", "", "")
		query.Type = "call"
		query.DeviceID = identity.DeviceID
		query.OwnerUserID = identity.OwnerUserID
		query.Arguments = map[string]json.RawMessage{"operation_id": json.RawMessage(`"` + call.CallID + `"`)}
		if err := wsjson.Write(ctx, conn, query); err != nil {
			t.Error(err)
			return
		}
		if err := wsjson.Read(ctx, conn, &result); err != nil {
			t.Error(err)
			return
		}
		replies <- result.Result
		_ = conn.Close(4001, "test_complete")
	})
	if err := client.Pair(context.Background(), base, "mosswork-pair.test", "PC", "windows"); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if _, err := client.AddGrant(context.Background(), dir, true, true); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		select {
		case result := <-replies:
			if result.Status != "completed" || result.Path != "report.docx" {
				t.Fatalf("%+v", result)
			}
		case <-time.After(3 * time.Second):
			t.Fatal("file call did not complete")
		}
	}
}

func TestServerRevocationClearsNativeGrantsAndIdentity(t *testing.T) {
	client, base := testServer(t, func(ctx context.Context, conn *websocket.Conn, h hello, identity ready) {
		var grants map[string]any
		if err := wsjson.Read(ctx, conn, &grants); err != nil {
			t.Error(err)
			return
		}
		_ = conn.Close(4001, "session_revoked")
	})
	if err := client.Pair(context.Background(), base, "mosswork-pair.test", "PC", "darwin"); err != nil {
		t.Fatal(err)
	}
	_, _ = client.AddGrant(context.Background(), t.TempDir(), true, true)
	deadline := time.Now().Add(3 * time.Second)
	for client.Status().State != "disconnected" && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	status := client.Status()
	if status.State != "disconnected" || status.OwnerUserID != 0 || status.DeviceID != "" || len(client.Grants()) != 0 {
		t.Fatalf("revoked native scope retained: %+v", status)
	}
}
