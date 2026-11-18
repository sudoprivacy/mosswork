// Package privatebootstrap consumes the existing Cloud login and Stack installer.
// It neither issues identities nor grants model credit or device membership.
package privatebootstrap

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

const CloudOrigin = "https://agent.sudoprivacy.com"
const maxResponse = 512 * 1024

type Client struct {
	origin string
	http   *http.Client
}

// Identity contains only public account metadata returned by the login authority.
type Identity struct {
	MemberID       string `json:"memberId"`
	OrganizationID string `json:"organizationId"`
}

// Configure consumes the original local-runtime projection on the native side.
// Implementations must send it to Stack via stdin, never arguments or the UI.
type Configure func(context.Context, Identity, []byte) error

func NewClient() *Client {
	return newClient(CloudOrigin, &http.Client{Timeout: 30 * time.Second})
}

func newClient(origin string, client *http.Client) *Client {
	// Do not inherit cookies, redirect policy or authentication from another
	// product client. Only the selected native login authority receives a token.
	c := *client
	c.Jar = nil
	c.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return &Client{origin: origin, http: &c}
}

func (c *Client) request(ctx context.Context, path string, body any, token string, out any) error {
	parsed, err := url.Parse(c.origin)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.Path != "" {
		return errors.New("Cloud login requires its configured HTTPS origin")
	}
	method := http.MethodGet
	var payload []byte
	if body != nil {
		method = http.MethodPost
		payload, err = json.Marshal(body)
		if err != nil {
			return errors.New("Invalid Cloud login request")
		}
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, c.origin+path, bytes.NewReader(payload))
	if err != nil {
		return errors.New("Invalid Cloud login request")
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := c.http.Do(req)
	if err != nil {
		return errors.New("Cloud login is unavailable; check the connection and retry manually")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("Cloud request was not accepted (HTTP %d)", response.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxResponse+1))
	if err != nil || len(data) > maxResponse || json.Unmarshal(data, out) != nil {
		// A parse error or upstream error body may contain a credential.
		return errors.New("Cloud returned an invalid response")
	}
	return nil
}

func (c *Client) PhoneLoginAvailable(ctx context.Context) (bool, error) {
	var result struct {
		Success bool `json:"success"`
		Data    struct {
			AuthMethods []string `json:"auth_methods"`
		} `json:"data"`
	}
	if err := c.request(ctx, "/api/v1/system-config", nil, "", &result); err != nil {
		return false, err
	}
	if !result.Success {
		return false, errors.New("Cloud login configuration is unavailable")
	}
	for _, method := range result.Data.AuthMethods {
		if method == "phone" {
			return true, nil
		}
	}
	return false, nil
}

var phonePattern = regexp.MustCompile(`^(?:\+?86)?1[3-9][0-9]{9}$`)
var codePattern = regexp.MustCompile(`^[0-9]{4,8}$`)
var identityPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

func (c *Client) SendCode(ctx context.Context, phone string) (int, error) {
	if !phonePattern.MatchString(phone) {
		return 0, errors.New("Enter a valid phone number")
	}
	var result struct {
		Success    bool `json:"success"`
		NextSendIn int  `json:"next_send_in"`
	}
	if err := c.request(ctx, "/api/v1/auth/send-code", map[string]string{"phone": phone}, "", &result); err != nil {
		return 0, err
	}
	if !result.Success || result.NextSendIn < 0 {
		return 0, errors.New("Code delivery was not confirmed; do not resend automatically")
	}
	return result.NextSendIn, nil
}

// LoginAndConfigure keeps the human token inside this native call. It reads a
// fresh projection using that token and binds it to the authenticated member.
// No signup, password reset, funding, retry or inferred permission occurs here.
func (c *Client) LoginAndConfigure(ctx context.Context, phone, code string, configure Configure) (Identity, error) {
	var identity Identity
	if !phonePattern.MatchString(phone) || !codePattern.MatchString(code) || configure == nil {
		return identity, errors.New("Enter the phone number and verification code")
	}
	var login struct {
		Success bool `json:"success"`
		Data    struct {
			AccessToken string `json:"access_token"`
			User        struct {
				ID    string `json:"id"`
				OrgID string `json:"orgId"`
			} `json:"user"`
		} `json:"data"`
	}
	if err := c.request(ctx, "/api/v1/auth/login", map[string]string{"phone": phone, "code": code}, "", &login); err != nil {
		return identity, err
	}
	identity = Identity{login.Data.User.ID, login.Data.User.OrgID}
	if !login.Success || login.Data.AccessToken == "" || strings.ContainsAny(login.Data.AccessToken, "\r\n") || !identityPattern.MatchString(identity.MemberID) || !identityPattern.MatchString(identity.OrganizationID) {
		return Identity{}, errors.New("Cloud did not return an authenticated account")
	}
	var projection map[string]json.RawMessage
	if err := c.request(ctx, "/api/v1/client/local-runtime", nil, login.Data.AccessToken, &projection); err != nil {
		return Identity{}, err
	}
	// Pass only the maintained model projection, never extra human-session fields.
	selected := make(map[string]json.RawMessage)
	for _, field := range []string{"execution", "localRuntime", "sudorouter_key", "model_service_url", "models", "scode_auto_model"} {
		value, exists := projection[field]
		if !exists {
			return Identity{}, errors.New("Cloud model access is not ready")
		}
		selected[field] = value
	}
	var runtime struct {
		UserID         string `json:"userId"`
		OrganizationID string `json:"organizationId"`
		Status         string `json:"status"`
	}
	var execution struct {
		IsLocalAllowed bool `json:"isLocalAllowed"`
	}
	if json.Unmarshal(selected["localRuntime"], &runtime) != nil || json.Unmarshal(selected["execution"], &execution) != nil || runtime.Status != "ready" || !execution.IsLocalAllowed || runtime.UserID != identity.MemberID || runtime.OrganizationID != identity.OrganizationID {
		return Identity{}, errors.New("Cloud did not authorize local setup for this account")
	}
	// These two objects carry account assertions, not arbitrary server fields.
	// Do not forward a future nested human-session field to the child process.
	selected["localRuntime"], _ = json.Marshal(runtime)
	selected["execution"], _ = json.Marshal(execution)
	data, err := json.Marshal(selected)
	if err != nil {
		return Identity{}, errors.New("Cloud returned an invalid model projection")
	}
	if err := ctx.Err(); err != nil {
		return Identity{}, err
	}
	if err := configure(ctx, identity, data); err != nil {
		return Identity{}, err
	}
	return identity, nil
}
