package company

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"
)

type CaptchaConfig struct {
	Canonical bool   `json:"canonical,omitempty"`
	Provider  string `json:"provider"`
	SiteKey   string `json:"site_key,omitempty"`
}

type LoginInput struct {
	Address      string `json:"address"`
	Username     string `json:"username"`
	Password     string `json:"password"`
	CaptchaToken string `json:"captcha_token,omitempty"`
	Remember     bool   `json:"remember"`
}

type LoginResult struct {
	AccessToken string `json:"access_token"`
}

var errAuthNotFound = errors.New("找不到登录服务 / Login service not found")
var errAuthUnauthorized = errors.New("账号或密码错误 / Incorrect username or password")

type IdentityStatus struct {
	Mode     string `json:"mode"`
	Phone    bool   `json:"phone_login_available"`
	Password bool   `json:"password_login_available"`
}

// Only these fixed authentication endpoints are used. Redirects are disabled by
// NewClient: credentials must never follow a redirect to another server.
func (c *Client) authJSON(ctx context.Context, base, path string, body any, output any) error {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	method := http.MethodGet
	var payload []byte
	if body != nil {
		method = http.MethodPost
		payload, _ = json.Marshal(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, base+path, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	response, err := c.httpClient.Do(req)
	if err != nil {
		return errors.New("无法连接服务器，请检查地址和网络 / Unable to reach the server")
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 64*1024+1))
	if err != nil || len(data) > 64*1024 {
		return errors.New("服务器响应无效 / Invalid server response")
	}
	if response.StatusCode != http.StatusOK {
		switch response.StatusCode {
		case http.StatusNotFound:
			return errAuthNotFound
		case 401:
			return errAuthUnauthorized
		case 429:
			return errors.New("尝试次数过多，请稍后重试 / Too many attempts; try again later")
		case 400, 403:
			return errors.New("登录未通过，请检查账号状态和验证结果 / Login rejected; check your account and verification")
		case 503:
			return errors.New("服务器尚未就绪，请联系管理员 / Server is not ready; contact your administrator")
		default:
			return errors.New("登录服务响应异常 / Unexpected login service response")
		}
	}
	if json.Unmarshal(data, output) != nil {
		return errors.New("服务器响应无效 / Invalid server response")
	}
	return nil
}

// Older employee servers can protect unknown API paths with a blanket 401.
// Recognize them only through their separate, public setup and captcha APIs.
func (c *Client) signInOptions(ctx context.Context, base string, needCaptcha bool) (IdentityStatus, CaptchaConfig, error) {
	var status struct {
		Mode     string `json:"mode"`
		Phone    *bool  `json:"phone_login_available"`
		Password *bool  `json:"password_login_available"`
	}
	err := c.authJSON(ctx, base, "/api/identity/status", nil, &status)
	var identity IdentityStatus
	var config CaptchaConfig
	if err == nil {
		if (status.Mode != "canonical" && status.Mode != "standalone") || status.Phone == nil || status.Password == nil {
			return identity, config, errors.New("无法确认服务器的登录方式 / Invalid sign-in configuration")
		}
		identity = IdentityStatus{Mode: status.Mode, Phone: *status.Phone, Password: *status.Password}
		if status.Mode == "canonical" || !needCaptcha {
			return identity, CaptchaConfig{Canonical: status.Mode == "canonical"}, nil
		}
	} else if !errors.Is(err, errAuthUnauthorized) && !errors.Is(err, errAuthNotFound) {
		return identity, config, err
	}
	if _, err = c.CheckServer(ctx, base); err != nil {
		return identity, config, err
	}
	err = c.authJSON(ctx, base, "/api/auth/captcha", nil, &config)
	config.Canonical = false
	if err == nil && config.Provider == "" {
		err = errors.New("服务器未返回登录验证配置 / Missing login verification configuration")
	}
	if err == nil && identity.Mode == "" {
		identity.Mode = "standalone"
	}
	return identity, config, err
}

func (c *Client) IdentityMode(ctx context.Context, base string) (IdentityStatus, error) {
	status, _, err := c.signInOptions(ctx, base, false)
	return status, err
}

func (c *Client) LoginConfig(ctx context.Context, raw string) (CaptchaConfig, error) {
	base, err := ServerAddress(raw)
	if err != nil {
		return CaptchaConfig{}, err
	}
	_, config, err := c.signInOptions(ctx, base, true)
	return config, err
}

func (c *Client) Login(ctx context.Context, input LoginInput) (string, LoginResult, error) {
	var result LoginResult
	base, err := ServerAddress(input.Address)
	if err != nil {
		return "", result, err
	}
	if strings.TrimSpace(input.Username) == "" || input.Password == "" || len(input.Username) > 320 || len(input.Password) > 4096 || len(input.CaptchaToken) > 4096 {
		return "", result, errors.New("请填写有效的账号和密码 / Enter your username and password")
	}
	status, err := c.IdentityMode(ctx, base)
	if err != nil {
		return "", result, err
	}
	if status.Mode == "canonical" {
		return "", result, errors.New("请在工作台完成账号登录 / Continue in the workspace to sign in")
	}
	err = c.authJSON(ctx, base, "/api/auth/login", map[string]string{
		"username": strings.TrimSpace(input.Username), "password": input.Password, "captcha_token": input.CaptchaToken,
	}, &result)
	if err == nil && result.AccessToken == "" {
		err = errors.New("服务器未返回登录凭证 / Missing login session")
	}
	return base, result, err
}
