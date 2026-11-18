package privatebootstrap

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const member = "f0000000-0000-4000-8000-000000000001"
const org = "f0000000-0000-4000-8000-000000000002"

func projection() map[string]any {
	return map[string]any{
		"execution":      map[string]any{"isLocalAllowed": true},
		"localRuntime":   map[string]any{"status": "ready", "userId": member, "organizationId": org},
		"sudorouter_key": "fixture-inference-only", "model_service_url": "https://router.invalid/v1",
		"models": []string{"fixture-model"}, "scode_auto_model": "fixture-model",
	}
}

func TestOriginalLoginProjectionAndNativeOnlyHandoff(t *testing.T) {
	var calls []string
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		if r.Header.Get("Cookie") != "" {
			t.Error("inherited a cookie")
		}
		switch r.URL.Path {
		case "/api/v1/system-config":
			fmt.Fprint(w, `{"success":true,"data":{"auth_methods":["phone"]}}`)
		case "/api/v1/auth/send-code":
			var body map[string]string
			json.NewDecoder(r.Body).Decode(&body)
			if body["phone"] != "13800000000" || len(body) != 1 {
				t.Error("wrong send-code request")
			}
			fmt.Fprint(w, `{"success":true,"next_send_in":60}`)
		case "/api/v1/auth/login":
			if r.Header.Get("Authorization") != "" {
				t.Error("prelogin auth leaked")
			}
			var body map[string]string
			json.NewDecoder(r.Body).Decode(&body)
			if body["phone"] != "13800000000" || body["code"] != "123456" || len(body) != 2 {
				t.Error("wrong login request")
			}
			json.NewEncoder(w).Encode(map[string]any{"success": true, "data": map[string]any{"access_token": "fixture-human-only", "refresh_token": "fixture-refresh-only", "user": map[string]string{"id": member, "orgId": org}}})
		case "/api/v1/client/local-runtime":
			if r.Header.Get("Authorization") != "Bearer fixture-human-only" {
				t.Error("missing native bearer")
			}
			data := projection()
			data["access_token"] = "fixture-human-only"
			data["refresh_token"] = "fixture-refresh-only"
			data["localRuntime"].(map[string]any)["humanSession"] = "fixture-human-only"
			data["execution"].(map[string]any)["session"] = "fixture-refresh-only"
			json.NewEncoder(w).Encode(data)
		default:
			t.Error("unexpected request", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	client := newClient(server.URL, server.Client())
	if ok, err := client.PhoneLoginAvailable(context.Background()); err != nil || !ok {
		t.Fatal(ok, err)
	}
	if wait, err := client.SendCode(context.Background(), "13800000000"); err != nil || wait != 60 {
		t.Fatal(wait, err)
	}
	configured := 0
	identity, err := client.LoginAndConfigure(context.Background(), "13800000000", "123456", func(ctx context.Context, got Identity, data []byte) error {
		configured++
		if got.MemberID != member || got.OrganizationID != org {
			t.Error("identity changed")
		}
		if bytes.Contains(data, []byte("human-only")) || bytes.Contains(data, []byte("refresh-only")) || !bytes.Contains(data, []byte("inference-only")) {
			t.Error("wrong credential handoff")
		}
		return nil
	})
	if err != nil || configured != 1 {
		t.Fatal(err, configured)
	}
	public, _ := json.Marshal(identity)
	if bytes.Contains(public, []byte("fixture-")) {
		t.Fatal("credential exposed to UI")
	}
	expected := []string{"GET /api/v1/system-config", "POST /api/v1/auth/send-code", "POST /api/v1/auth/login", "GET /api/v1/client/local-runtime"}
	if strings.Join(calls, "\n") != strings.Join(expected, "\n") {
		t.Fatal("retry or extra operation", calls)
	}
}

func TestProjectionMustBelongToAuthenticatedAccount(t *testing.T) {
	for _, variant := range []string{"different-member", "different-org", "denied", "pending", "missing-key", "malformed", "withdrawn"} {
		t.Run(variant, func(t *testing.T) {
			calls := 0
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				if r.URL.Path == "/api/v1/auth/login" {
					fmt.Fprintf(w, `{"success":true,"data":{"access_token":"secret-human","user":{"id":%q,"orgId":%q}}}`, member, org)
					return
				}
				data := projection()
				switch variant {
				case "different-member":
					data["localRuntime"].(map[string]any)["userId"] = org
				case "different-org":
					data["localRuntime"].(map[string]any)["organizationId"] = member
				case "denied":
					data["execution"].(map[string]any)["isLocalAllowed"] = false
				case "pending":
					data["localRuntime"].(map[string]any)["status"] = "credential_pending"
				case "missing-key":
					delete(data, "sudorouter_key")
				case "malformed":
					fmt.Fprint(w, `{secret-human`)
					return
				case "withdrawn":
					w.WriteHeader(403)
					fmt.Fprint(w, "secret-human")
					return
				}
				json.NewEncoder(w).Encode(data)
			}))
			defer server.Close()
			client := newClient(server.URL, server.Client())
			_, err := client.LoginAndConfigure(context.Background(), "13800000000", "123456", func(context.Context, Identity, []byte) error { t.Error("denied projection installed"); return nil })
			if err == nil || strings.Contains(err.Error(), "secret-human") || calls != 2 {
				t.Fatal(err, calls)
			}
		})
	}
}

func TestRedirectNeverReceivesPhoneCodeOrBearer(t *testing.T) {
	trap := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("followed redirect") }))
	defer trap.Close()
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, trap.URL, 307) }))
	defer server.Close()
	client := newClient(server.URL, server.Client())
	_, err := client.SendCode(context.Background(), "13800000000")
	if err == nil || !strings.Contains(err.Error(), "307") {
		t.Fatal(err)
	}
}

func TestCancelledLoginNeverStartsInstaller(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	client := NewClient()
	_, err := client.LoginAndConfigure(ctx, "13800000000", "123456", func(context.Context, Identity, []byte) error { t.Error("cancelled install started"); return nil })
	if err == nil {
		t.Fatal("cancel ignored")
	}
}
