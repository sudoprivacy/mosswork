package company

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestEmployeeLoginOverHTTP(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.URL.RawQuery != "" {
			t.Error("credentials in metadata")
		}
		switch r.URL.Path {
		case "/api/identity/status":
			http.NotFound(w, r)
		case "/api/setup/status":
			w.Write([]byte(`{"setup_required":false}`))
		case "/api/auth/captcha":
			w.Write([]byte(`{"provider":"slider"}`))
		case "/api/auth/login":
			var body map[string]string
			if r.Method != "POST" || json.NewDecoder(r.Body).Decode(&body) != nil || body["username"] != "employee" || body["password"] != "sample-password" || body["captcha_token"] != "verification" {
				t.Error("invalid login body")
			}
			w.Write([]byte(`{"access_token":"test-session"}`))
		default:
			t.Error("unexpected endpoint")
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := NewClient(t.TempDir())
	address := server.URL
	config, err := client.LoginConfig(context.Background(), address)
	if err != nil || config.Provider != "slider" {
		t.Fatal(config, err)
	}
	base, result, err := client.Login(context.Background(), LoginInput{Address: address, Username: "employee", Password: "sample-password", CaptchaToken: "verification"})
	if err != nil || base != server.URL || result.AccessToken != "test-session" {
		t.Fatal("login failed", err)
	}
}

func TestEmployeeLoginDoesNotRedirectCredentials(t *testing.T) {
	visited := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { visited = true }))
	defer target.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/identity/status" {
			w.Write([]byte(`{"mode":"standalone","phone_login_available":false,"password_login_available":false}`))
			return
		}
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer server.Close()
	_, _, err := NewClient(t.TempDir()).Login(context.Background(), LoginInput{Address: server.URL, Username: "employee", Password: "sample-password"})
	if err == nil || visited {
		t.Fatal("followed login redirect")
	}
}

func TestCanonicalWorkspaceNegotiation(t *testing.T) {
	for _, tc := range []struct {
		name      string
		code      int
		body      string
		canonical bool
		valid     bool
	}{
		{"organization", 200, `{"mode":"canonical","phone_login_available":true,"password_login_available":false}`, true, true},
		{"standalone", 200, `{"mode":"standalone","phone_login_available":false,"password_login_available":true}`, false, true},
		{"older server protects unknown routes", 401, `{}`, false, true},
		{"incomplete", 200, `{"mode":"canonical"}`, false, false},
		{"unavailable", 503, "unavailable", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logins := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/api/identity/status":
					if r.Method != "GET" || r.ContentLength > 0 || r.Header.Get("Authorization") != "" || r.URL.RawQuery != "" {
						t.Error("public identity lookup carried credentials")
					}
					w.WriteHeader(tc.code)
					w.Write([]byte(tc.body))
				case "/api/setup/status", "/api/auth/captcha", "/api/auth/login":
					if tc.canonical || !tc.valid {
						t.Error("unexpected legacy request", r.URL.Path)
					}
					if r.URL.Path == "/api/auth/login" {
						logins++
					}
					w.Write([]byte(`{"setup_required":false,"provider":"slider","access_token":"test-session"}`))
				default:
					t.Error("unexpected request", r.URL.Path)
				}
			}))
			defer server.Close()
			client := NewClient(t.TempDir())
			config, err := client.LoginConfig(context.Background(), server.URL)
			if (err == nil) != tc.valid || config.Canonical != tc.canonical {
				t.Fatal(config, err)
			}
			_, _, err = client.Login(context.Background(), LoginInput{Address: server.URL, Username: "employee", Password: "test-password"})
			legacy := tc.valid && !tc.canonical
			if (err == nil) != legacy || (logins == 1) != legacy {
				t.Fatal("unexpected legacy password submission", logins, err)
			}
		})
	}
}

func TestEmployeeLoginErrorsAreBoundedAndDoNotEchoSecrets(t *testing.T) {
	for _, status := range []int{200, 401, 403, 429, 503} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/api/identity/status" {
					w.Write([]byte(`{"mode":"standalone","phone_login_available":false,"password_login_available":false}`))
					return
				}
				w.WriteHeader(status)
				w.Write([]byte(`{"detail":"sample-password"}`))
			}))
			defer server.Close()
			_, _, err := NewClient(t.TempDir()).Login(context.Background(), LoginInput{Address: server.URL, Username: "employee", Password: "sample-password"})
			if err == nil || strings.Contains(err.Error(), "sample-password") {
				t.Fatal("invalid or unsafe error")
			}
		})
	}
}
