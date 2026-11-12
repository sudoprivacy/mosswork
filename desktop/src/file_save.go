package main

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"

	"github.com/wailsapp/wails/v3/pkg/application"
)

type saveBlobMessage struct {
	Type      string `json:"type"`
	RequestID string `json:"requestId"`
	Filename  string `json:"filename"`
	Encoded   string `json:"encoded"`
}

// handleRawMessage is the fallback bridge for pages loaded from a remote
// company origin. Such pages cannot import the Wails runtime at /wails/runtime.js,
// but the native webview still delivers postMessage messages to the shell.
func (a *App) handleRawMessage(window application.Window, message string, _ *application.OriginInfo) {
	var request saveBlobMessage
	if json.Unmarshal([]byte(message), &request) != nil || request.Type != "mosswork:save-blob" || request.RequestID == "" {
		return
	}
	path, err := a.SaveBlobFile(request.Filename, request.Encoded)
	result, _ := json.Marshal(map[string]string{"path": path})
	if err != nil {
		result, _ = json.Marshal(map[string]string{"error": err.Error()})
	}
	callback, _ := json.Marshal(request.RequestID)
	window.ExecJS("window.__mossworkSaveBlobResult(" + string(callback) + "," + string(result) + ");")
}

// SaveBlobFile opens the native save dialog and writes a user-selected file.
// The dashboard passes authenticated response bytes only after the export
// request succeeds; the user still chooses the destination explicitly.
func (a *App) SaveBlobFile(filename, encoded string) (string, error) {
	if a.app == nil || a.window == nil {
		return "", errors.New("desktop window is unavailable")
	}
	name := filepath.Base(strings.TrimSpace(filename))
	if name == "." || name == string(filepath.Separator) || name == "" {
		return "", errors.New("invalid filename")
	}
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return "", errors.New("invalid file data")
	}
	path, err := a.app.Dialog.SaveFile().
		SetFilename(name).
		AddFilter("Excel workbook", "*.xlsx").
		AttachToWindow(a.window).
		PromptForSingleSelection()
	if err != nil {
		return "", err
	}
	if path == "" {
		return "", nil
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return "", err
	}
	return path, nil
}
