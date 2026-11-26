package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
)

var companyCreateThreadTimeout = 30 * time.Second

func (a *App) companyCreateThread(nonce, base string, params json.RawMessage) (any, error) {
	var input struct {
		AgentID string `json:"agent_id"`
	}
	if err := json.Unmarshal(params, &input); err != nil || strings.TrimSpace(input.AgentID) == "" {
		return nil, errors.New("invalid_create_thread_request")
	}

	ctx, cancel := context.WithTimeout(context.Background(), companyCreateThreadTimeout)
	defer cancel()
	body, err := json.Marshal(map[string]any{
		"agent_id":       input.AgentID,
		"execution_mode": "local",
	})
	if err != nil {
		return nil, err
	}
	a.companyMu.Lock()
	if a.workspaceNonce != nonce || a.companyURL != base {
		a.companyMu.Unlock()
		return nil, errors.New("runtime_scope_changed")
	}
	client := a.company
	a.companyMu.Unlock()
	status, response, err := client.CompanyPost(
		ctx,
		base,
		"/api/company-runtime/threads",
		body,
	)
	if err != nil {
		switch err.Error() {
		case "device_session_expired", "device_disconnected", "device_unpaired", "device_reconnecting_timeout":
			return nil, &devicePreparationError{err}
		}
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return nil, errors.New("公司服务器响应超时，请重试")
		}
		return nil, errors.New("公司连接不可用，请重新连接")
	}
	if status < 200 || status >= 300 {
		reason := companyThreadErrorReason(status, response)
		if status == http.StatusUnauthorized && reason == "device_session_expired" {
			return nil, &devicePreparationError{errors.New(reason)}
		}
		return nil, errors.New(reason)
	}
	var result map[string]any
	if err := json.Unmarshal(response, &result); err != nil {
		return nil, fmt.Errorf("company_runtime_invalid_response: %w", err)
	}
	return result, nil
}

func companyThreadErrorReason(status int, response []byte) string {
	var envelope struct {
		Error struct {
			Code    string         `json:"code"`
			Details map[string]any `json:"details"`
		} `json:"error"`
	}
	if json.Unmarshal(response, &envelope) == nil {
		if reason, ok := envelope.Error.Details["reason"].(string); ok && reason != "" {
			return reason
		}
		if envelope.Error.Code != "" {
			return envelope.Error.Code
		}
	}
	return fmt.Sprintf("company_runtime_http_%d", status)
}
