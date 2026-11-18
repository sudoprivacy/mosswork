package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

type threadDirectoryEntry struct {
	Name  string `json:"name"`
	IsDir bool   `json:"is_dir"`
}

type threadDirectoryListing struct {
	Root    string                 `json:"root"`
	Entries []threadDirectoryEntry `json:"entries"`
}

// Both the server binding and a receipt belonging to this account on this Mac/PC
// must agree before a WebView request can browse the granted project.
func (a *App) threadWorkspace(nonce, base string, raw json.RawMessage) (any, error) {
	var p struct {
		Thread string `json:"thread_id"`
		Agent  string `json:"agent_id"`
		Path   string `json:"path"`
		Token  string `json:"token"`
		Reveal bool   `json:"reveal"`
	}
	if json.Unmarshal(raw, &p) != nil || !forwardMediaID.MatchString(p.Thread) || !forwardMediaID.MatchString(p.Agent) || p.Token == "" || len(p.Token) > 16384 || strings.ContainsAny(p.Token, "\r\n") {
		return nil, errors.New("会话目录请求无效")
	}
	a.companyMu.Lock()
	workspace, epoch := a.workspacePath, a.runtimeEpoch
	if a.company == nil || !a.runtimePreviewScopeCurrent(nonce, base, workspace, epoch) {
		a.companyMu.Unlock()
		return nil, errors.New("请先授权当前账号的本地工作目录")
	}
	identity, _, err := a.company.RuntimeIdentity()
	a.companyMu.Unlock()
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/agents/"+p.Agent+"/threads/"+p.Thread+"/workspace", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+p.Token)
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(req)
	if err != nil {
		return nil, errors.New("无法连接公司服务以核对会话目录")
	}
	defer response.Body.Close()
	var binding struct {
		Location string
		Root     string
		Task     string `json:"task_id"`
	}
	if response.StatusCode != http.StatusOK || json.NewDecoder(io.LimitReader(response.Body, 1<<16)).Decode(&binding) != nil || binding.Location != "desktop" || !forwardMediaID.MatchString(binding.Task) {
		return nil, errors.New("无法核对本机会话目录")
	}
	var receipt struct {
		Thread  string `json:"thread_id"`
		Agent   string `json:"agent_id"`
		Project string `json:"project_path"`
	}
	receiptData, err := os.ReadFile(filepath.Join(companyStateDir(base, identity.OwnerUserID), "tasks", binding.Task, "task.json"))
	if err != nil || json.Unmarshal(receiptData, &receipt) != nil || receipt.Thread != p.Thread || receipt.Agent != p.Agent {
		return nil, errors.New("请在运行此会话的电脑上打开工作区")
	}
	parent, err := filepath.EvalSymlinks(workspace)
	if err != nil || filepath.Clean(filepath.Dir(receipt.Project)) != filepath.Clean(parent) {
		return nil, errors.New("请切换到运行此会话时授权的工作目录")
	}
	listing, target, isDir, err := browseThreadDirectory(parent, binding.Root, p.Path, p.Reveal)
	if err != nil {
		return nil, err
	}
	a.companyMu.Lock()
	defer a.companyMu.Unlock()
	if !a.runtimePreviewScopeCurrent(nonce, base, workspace, epoch) {
		return nil, errors.New("工作目录已切换，请重新打开会话")
	}
	if !p.Reveal {
		return listing, nil
	}
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		if isDir {
			cmd = exec.Command("/usr/bin/open", target)
		} else {
			cmd = exec.Command("/usr/bin/open", "-R", target)
		}
	case "windows":
		if isDir {
			cmd = exec.Command("explorer.exe", target)
		} else {
			cmd = exec.Command("explorer.exe", "/select,"+target)
		}
	default:
		return nil, errors.New("当前系统不支持打开文件位置")
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return nil, cmd.Process.Release()
}

func browseThreadDirectory(workspace, project, path string, reveal bool) (threadDirectoryListing, string, bool, error) {
	result := threadDirectoryListing{}
	invalid := errors.New("会话目录或文件不可用")
	if !filepath.IsLocal(project) || filepath.Base(project) != project || strings.ContainsAny(project, "/\\:") || project == "." {
		return result, "", false, invalid
	}
	if path == "" {
		path = "."
	}
	if !fs.ValidPath(path) || strings.ContainsAny(path, "\\:") {
		return result, "", false, invalid
	}
	relative, err := filepath.Localize(path)
	if err != nil {
		return result, "", false, invalid
	}
	grant, err := os.OpenRoot(workspace)
	if err != nil {
		return result, "", false, invalid
	}
	defer grant.Close()
	info, err := grant.Lstat(project)
	if err != nil || !info.IsDir() {
		return result, "", false, invalid
	}
	root, err := grant.OpenRoot(project)
	if err != nil {
		return result, "", false, invalid
	}
	defer root.Close()
	actual, err := root.Stat(".")
	if err != nil || !os.SameFile(info, actual) {
		return result, "", false, invalid
	}
	target := filepath.Join(workspace, project, relative)
	canonical, err := filepath.EvalSymlinks(target)
	if err != nil || canonical != target {
		return result, "", false, invalid
	}
	info, err = root.Lstat(relative)
	if err != nil || (!info.IsDir() && !(reveal && info.Mode().IsRegular())) {
		return result, "", false, invalid
	}
	result.Root = filepath.Join(workspace, project)
	result.Entries = []threadDirectoryEntry{}
	if !reveal {
		entries, err := fs.ReadDir(root.FS(), path)
		if err != nil {
			return result, "", false, invalid
		}
		for _, entry := range entries {
			if entry.IsDir() || entry.Type().IsRegular() {
				result.Entries = append(result.Entries, threadDirectoryEntry{Name: entry.Name(), IsDir: entry.IsDir()})
			}
		}
	}
	return result, target, info.IsDir(), nil
}
