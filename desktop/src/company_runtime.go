package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"mosswork.desktop/internal/companyruntime"
)

const runtimeEventPrefix = "desktop:runtime:"

func companyStateDir(base string, owner int64) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s\x00%d", base, owner)))
	return filepath.Join(mossworkHome(), "company-runtime", "accounts", hex.EncodeToString(sum[:]))
}
func parseRuntimeEvent(name, nonce string) (id, method string, params json.RawMessage, ok bool) {
	if nonce == "" || !strings.HasPrefix(name, runtimeEventPrefix) || len(name) > 2*1024*1024 {
		return
	}
	p := strings.SplitN(strings.TrimPrefix(name, runtimeEventPrefix), ":", 4)
	if len(p) != 4 || p[0] != nonce || len(p[1]) == 0 || len(p[1]) > 32 || strings.IndexFunc(p[1], func(r rune) bool { return r < '0' || r > '9' }) >= 0 {
		return
	}
	switch p[2] {
	case "status", "thread_workspace", "refresh_status", "start", "cancel", "reveal", "reconcile", "preview_image", "steer", "steer_remove", "hitl_resume", "upload_artifact", "import_attachment", "create_thread":
	default:
		return
	}
	raw, err := url.QueryUnescape(p[3])
	if err != nil || !json.Valid([]byte(raw)) || !strings.HasPrefix(raw, "{") {
		return
	}
	return p[1], p[2], json.RawMessage(raw), true
}
func (a *App) stopCompanyRuntimeLocked() {
	if a.companyRuntime != nil && a.window != nil && a.workspaceNonce != "" {
		a.window.ExecJS(`(function(origin,nonce){if(location.origin===origin)window.__mossworkRuntimeEvent?.(nonce,{event:"runtime_exit",status:"unknown",error:"工作目录或账号已切换，本地任务已停止；请核对已生成文件。"});})(` + jsonString(a.companyPageOriginLocked()) + `,` + jsonString(a.workspaceNonce) + `)`)
	}
	a.runtimeEpoch++
	if a.companyRuntime != nil {
		a.companyRuntime.Close()
		a.companyRuntime = nil
	}
	a.runtimeArtifacts = map[string]bool{}
}
func (a *App) dispatchRuntimeEvent(name string) {
	a.companyMu.Lock()
	nonce, base, epoch := a.workspaceNonce, a.companyURL, a.runtimeEpoch
	id, method, params, ok := parseRuntimeEvent(name, nonce)
	a.companyMu.Unlock()
	if !ok || base == "" {
		return
	}
	go func() {
		result, err := a.performRuntimeAction(nonce, base, method, params)
		a.companyMu.Lock()
		defer a.companyMu.Unlock()
		if a.workspaceNonce != nonce || a.companyURL != base {
			return
		}
		if a.runtimeEpoch != epoch {
			switch method {
			case "thread_workspace":
				result, err = nil, errors.New("工作目录已切换，请重新打开会话")
			case "preview_image", "upload_artifact", "import_attachment":
				return
			}
		}
		payload := map[string]any{"id": id, "result": result}
		if err != nil {
			code := runtimeErrorCode(err.Error())
			payload["error"] = runtimeErrorText(code)
			payload["error_code"] = code
			payload["stage"] = runtimeErrorStage(code)
			payload["retryable"] = runtimeErrorRetryable(code)
			if action := runtimeErrorAction(code); action != "" {
				payload["action"] = action
			}
		}
		data, _ := json.Marshal(payload)
		a.window.ExecJS(`(function(origin,nonce,result){if(location.origin===origin)window.__mossworkRuntimeReply?.(nonce,result);})(` + jsonString(a.companyPageOriginLocked()) + `,` + jsonString(nonce) + `,` + string(data) + `)`)
	}()
}
func (a *App) performRuntimeAction(nonce, base, method string, params json.RawMessage) (any, error) {
	// Share the updater's admission fence with every native task action. The
	// restart status check cannot race a start/approval/attachment operation.
	a.updateGate.RLock()
	defer a.updateGate.RUnlock()
	if a.updateClosing.Load() {
		return nil, errors.New("客户端即将更新，请在重启后继续操作")
	}
	a.companyMu.Lock()
	if a.workspaceNonce != nonce || a.companyURL != base {
		a.companyMu.Unlock()
		return nil, errors.New("账号已切换，请重新打开当前会话")
	}
	if method == "thread_workspace" {
		a.companyMu.Unlock()
		return a.threadWorkspace(nonce, base, params)
	}
	if method == "refresh_status" {
		// A settings refresh must work before pairing a project and must never
		// initialize a runtime just to discover whether it has active work.
		process := a.companyRuntime
		a.companyMu.Unlock()
		if process == nil || !process.Alive() {
			return map[string]any{"active_tasks": []any{}}, nil
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return process.Request(ctx, "status", json.RawMessage(`{}`))
	}
	if method == "upload_artifact" || method == "import_attachment" {
		a.companyMu.Unlock()
		return a.transferRuntimeArtifact(nonce, base, method, params)
	}
	if method == "create_thread" {
		a.companyMu.Unlock()
		return a.companyCreateThread(nonce, base, params)
	}
	if method == "preview_image" {
		var p struct {
			Path   string `json:"path"`
			SHA256 string `json:"sha256"`
		}
		if json.Unmarshal(params, &p) != nil {
			a.companyMu.Unlock()
			return nil, errors.New("图片预览请求无效")
		}
		workspace, epoch, granted := a.workspacePath, a.runtimeEpoch, a.workspaceGrant != nil
		a.companyMu.Unlock()
		if !granted {
			return nil, errors.New("请先授权当前账号的本地工作目录")
		}
		preview, err := previewRuntimeImage(workspace, p.Path, p.SHA256)
		a.companyMu.Lock()
		defer a.companyMu.Unlock()
		if !a.runtimePreviewScopeCurrent(nonce, base, workspace, epoch) {
			return nil, errors.New("账号或工作目录已切换，图片预览已取消")
		}
		if err != nil {
			return nil, err
		}
		return preview, nil
	}
	if method == "reveal" {
		var p struct {
			Path   string `json:"path"`
			SHA256 string `json:"sha256"`
		}
		_ = json.Unmarshal(params, &p)
		workspace, granted := a.workspacePath, a.workspaceGrant != nil
		currentReceipt := a.runtimeArtifacts[p.Path]
		a.companyMu.Unlock()
		if !granted {
			return nil, errors.New("请先授权当前账号的本地工作目录")
		}
		path, err := checkedRuntimeArtifact(workspace, p.Path, p.SHA256, currentReceipt)
		if err != nil {
			return nil, err
		}
		a.companyMu.Lock()
		defer a.companyMu.Unlock()
		if a.workspaceNonce != nonce || a.companyURL != base || a.workspacePath != workspace {
			return nil, errors.New("账号或工作目录已切换，请重新打开当前账号文件")
		}
		if runtime.GOOS == "darwin" {
			return nil, exec.Command("/usr/bin/open", "-R", path).Run()
		}
		if runtime.GOOS == "windows" {
			c := exec.Command("explorer.exe", "/select,"+path)
			if err := c.Start(); err != nil {
				return nil, err
			}
			return nil, c.Process.Release()
		}
		return nil, errors.New("当前系统不支持打开文件位置")
	}
	a.companyMu.Unlock()
	if method == "status" {
		var options struct {
			VerifyTools bool `json:"verify_tools"`
		}
		if json.Unmarshal(params, &options) == nil && options.VerifyTools {
			root, err := ensureCompanyPortable()
			if err != nil {
				return nil, err
			}
			if err := validateCompanyRuntime(root); err != nil {
				readyCompanyPortable.invalidate(root)
				return nil, err
			}
		}
	}
	process, err := a.ensureRuntimeProcess(nonce, base, method, ensureCompanyPortable)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	return process.Request(ctx, method, params)
}

func runtimeErrorCode(value string) string {
	if strings.HasPrefix(value, "company_model_") || strings.HasPrefix(value, "device_") {
		return value
	}
	switch value {
	case "heartbeat_timeout", "workspace_file_locked", "workspace_permission_denied", "workspace_file_not_found", "task_result_unknown":
		return value
	default:
		return "local_runtime_error"
	}
}

func runtimeErrorStage(code string) string {
	switch {
	case strings.HasPrefix(code, "device_"), code == "heartbeat_timeout":
		return "device_connection"
	case strings.HasPrefix(code, "company_model_"):
		return "model_request"
	case strings.HasPrefix(code, "workspace_"):
		return "artifact_write"
	case code == "task_result_unknown":
		return "task_reconcile"
	default:
		return "runtime"
	}
}

func runtimeErrorRetryable(code string) bool {
	if strings.HasPrefix(code, "company_model_http_") {
		status, err := strconv.Atoi(strings.TrimPrefix(code, "company_model_http_"))
		return err == nil && (status == 408 || status == 429 || status >= 500)
	}
	if strings.HasPrefix(code, "company_model_") {
		return code != "company_model_context_overflow" && code != "company_model_empty_response"
	}
	return code == "workspace_file_locked" || code == "workspace_permission_denied" || code == "device_reconnecting_timeout"
}

func runtimeErrorAction(code string) string {
	switch code {
	case "workspace_file_locked":
		return "close_file_and_retry"
	case "workspace_permission_denied":
		return "check_file_permissions"
	case "task_result_unknown", "local_runtime_error":
		return "check_task_status"
	default:
		return ""
	}
}

// Runtime verification and initialization must never hold the session lock:
// native image reads and changing/revoking the account remain independent.
func (a *App) ensureRuntimeProcess(nonce, base, method string, prepare func() (string, error)) (*companyruntime.Process, error) {
	a.runtimeStartMu.Lock()
	defer a.runtimeStartMu.Unlock()
	a.companyMu.Lock()
	if a.workspaceNonce != nonce || a.companyURL != base {
		a.companyMu.Unlock()
		return nil, errors.New("账号已切换，请重新打开当前会话")
	}
	client := a.company
	a.companyMu.Unlock()
	// A transient reconnect must not be reported as a permanent device failure.
	// RuntimeIdentity waits briefly for the native channel to recover without
	// holding companyMu, so status polling and account changes remain responsive.
	identity, credential, err := client.RuntimeIdentity()
	if err != nil {
		return nil, err
	}
	a.companyMu.Lock()
	if a.company != client || a.workspaceNonce != nonce || a.companyURL != base {
		a.companyMu.Unlock()
		return nil, errors.New("账号已切换，请重新打开当前会话")
	}
	if a.workspacePath == "" || a.workspaceGrant == nil || a.workspaceError != "" {
		a.companyMu.Unlock()
		return nil, errors.New("请先在设置中选择本地工作目录并授权；终端将使用当前系统用户权限，工作目录不是安全沙箱")
	}
	if a.companyRuntime == nil || !a.companyRuntime.Alive() {
		if method == "cancel" || method == "hitl_resume" {
			a.companyMu.Unlock()
			return nil, errors.New("本地任务进程已退出；请核对文件和公司任务记录")
		}
		workspace, epoch := a.workspacePath, a.runtimeEpoch
		a.companyMu.Unlock()
		a.emitRuntimeProgress(nonce, base, epoch, "tools")
		root, err := prepare()
		if err != nil {
			return nil, err
		}
		stateDir := companyStateDir(base, identity.OwnerUserID)
		if err = os.MkdirAll(stateDir, 0700); err != nil {
			return nil, errors.New("无法创建当前账号的本地运行目录")
		}
		cacheDir := filepath.Join(mossworkHome(), "pyc")
		if os.MkdirAll(cacheDir, 0700) != nil {
			cacheDir = ""
		}
		env := companyruntime.Environment(stateDir, root, filepath.Dir(pythonExe(root))+string(os.PathListSeparator)+filepath.Join(root, "tools")+string(os.PathListSeparator)+os.Getenv("PATH"), cacheDir)
		a.companyMu.Lock()
		if !a.runtimePreviewScopeCurrent(nonce, base, workspace, epoch) {
			a.companyMu.Unlock()
			return nil, errors.New("账号或工作目录已切换，本地运行时启动已取消")
		}
		latest, key := client.CurrentLease()
		if key == "" || latest.DeviceID != identity.DeviceID || latest.OwnerUserID != identity.OwnerUserID {
			a.companyMu.Unlock()
			return nil, errors.New("device_disconnected")
		}
		credential = key
		events := make(chan companyruntime.Message, 512)
		go func() {
			for m := range events {
				a.deliverRuntimeEvent(nonce, base, epoch, m)
			}
		}()
		process, err := companyruntime.Start(pythonExe(root), filepath.Join(root, "launch.py"), workspace, env, func(m companyruntime.Message) {
			events <- m
			if m.Event == "runtime_exit" {
				close(events)
			}
		})
		if err != nil {
			close(events)
			a.companyMu.Unlock()
			readyCompanyPortable.invalidate(root)
			return nil, errors.New("本地运行时无法启动，请重新安装包含完整工具的企业客户端")
		}
		a.companyRuntime = process
		go a.watchRuntimeIdentity(nonce, base, epoch, credential)
		a.companyMu.Unlock()
		a.emitRuntimeProgress(nonce, base, epoch, "runtime")
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		_, err = process.Request(ctx, "initialize", map[string]any{"company_url": base, "credential": credential, "device_id": identity.DeviceID, "owner_user_id": identity.OwnerUserID, "workspace": workspace, "state_dir": stateDir, "execute_authorized": true})
		cancel()
		a.companyMu.Lock()
		if err != nil {
			// Account/workspace changes deliberately stop the old process and
			// must not invalidate otherwise unchanged shared tools.
			exited := a.companyRuntime == process && !process.Alive()
			if a.companyRuntime == process {
				a.stopCompanyRuntimeLocked()
			}
			a.companyMu.Unlock()
			if exited {
				readyCompanyPortable.invalidate(root)
			}
			return nil, err
		}
		if a.companyRuntime != process || !a.runtimePreviewScopeCurrent(nonce, base, workspace, epoch) {
			a.companyMu.Unlock()
			process.Close()
			return nil, errors.New("账号或工作目录已切换，本地运行时启动已取消")
		}
	}
	process := a.companyRuntime
	a.companyMu.Unlock()
	return process, nil
}

func (a *App) emitRuntimeProgress(nonce, base string, epoch uint64, phase string) {
	a.deliverRuntimeEvent(nonce, base, epoch, companyruntime.Message{
		Event: "runtime_progress",
		Phase: phase,
	})
}

func applyRuntimeErrorTranslation(m companyruntime.Message) companyruntime.Message {
	if m.ErrorCode == "" && m.Error != "" {
		m.ErrorCode = m.Error
	}
	if m.ErrorCode != "" {
		m.Error = runtimeErrorText(m.ErrorCode)
	}
	return m
}

func (a *App) deliverRuntimeEvent(nonce, base string, epoch uint64, m companyruntime.Message) {
	a.companyMu.Lock()
	defer a.companyMu.Unlock()
	if a.workspaceNonce != nonce || a.companyURL != base || a.runtimeEpoch != epoch || a.window == nil {
		return
	}
	if m.Event == "done" && len(m.Artifacts) > 0 {
		var files []struct {
			Path string `json:"path"`
		}
		if json.Unmarshal(m.Artifacts, &files) == nil {
			if a.runtimeArtifacts == nil {
				a.runtimeArtifacts = map[string]bool{}
			}
			for _, f := range files {
				if filepath.IsAbs(f.Path) {
					a.runtimeArtifacts[f.Path] = true
				}
			}
		}
	}
	m = applyRuntimeErrorTranslation(m)
	data, _ := json.Marshal(m)
	a.window.ExecJS(`(function(origin,nonce,event){if(location.origin===origin)window.__mossworkRuntimeEvent?.(nonce,event);})(` + jsonString(a.companyPageOriginLocked()) + `,` + jsonString(nonce) + `,` + string(data) + `)`)
}

func (a *App) watchRuntimeIdentity(nonce, base string, epoch uint64, credential string) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for range ticker.C {
		a.companyMu.Lock()
		if a.workspaceNonce != nonce || a.companyURL != base || a.runtimeEpoch != epoch || a.companyRuntime == nil || !a.companyRuntime.Alive() {
			a.companyMu.Unlock()
			return
		}
		status, key := a.company.CurrentLease()
		if status.State == "unpaired" || status.State == "disconnected" || time.Now().Unix() >= status.ExpiresAt {
			a.stopCompanyRuntimeLocked()
			a.companyMu.Unlock()
			return
		}
		process := a.companyRuntime
		a.companyMu.Unlock()
		if key != "" && key != credential {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			_, err := process.Request(ctx, "renew_credential", map[string]string{"credential": key})
			cancel()
			if err == nil {
				credential = key
			}
		}
	}
}

func runtimeErrorText(code string) string {
	messages := map[string]string{
		"execution_mode_server":                  "此会话已在网页端使用，请新建会话进行本机执行",
		"server_thread_running":                  "此会话正在服务器执行，请等待完成后再试",
		"project_binding_required":               "会话缺少本地项目绑定，请更新公司服务端后重试",
		"device_reconnecting_timeout":            "本机连接正在恢复，但等待已超时；请确认客户端仍在运行后重试。",
		"device_disconnected":                    "本机设备已断开公司服务，请检查网络和客户端连接后重试。",
		"device_unpaired":                        "本机设备尚未连接公司服务，请在设置中完成配对。",
		"device_session_expired":                 "本机设备授权已过期。请检查本地工作目录中的连接状态；只有账号登录也已过期时才需要重新登录。",
		"heartbeat_timeout":                      "本机心跳应答超时，正在重新连接公司服务。",
		"workspace_file_locked":                  "文件正在被其他程序占用，请关闭文件后再试。",
		"workspace_permission_denied":            "没有权限读取或写入该文件，请检查文件权限后再试。",
		"workspace_file_not_found":               "文件不存在或已被移动，请检查文件位置后再试。",
		"task_result_unknown":                    "任务结果暂时无法确认，请先检查已有成果，系统不会自动重复执行。",
		"company_runtime_disabled":               "公司尚未启用本机执行服务，请联系管理员开启；通讯录和员工聊天仍可使用。",
		"company_protocol_incompatible":          "公司服务与客户端运行协议不兼容，请安装匹配的企业客户端。",
		"legacy_thread_read_only":                "此会话包含服务器执行的旧历史，仅供查看；请新建会话开始本机任务。",
		"project_on_another_device":              "此会话的本地项目属于另一台电脑，文件未同步；请新建会话处理本机文件。",
		"task_selection_changed":                 "任务状态已变化，请刷新任务列表后再核对。",
		"recovery_not_owned":                     "无法核对该任务的归属，请使用原公司账号、原电脑和原工作目录重新连接。",
		"previous_device_still_active":           "该任务仍连接到另一会话，请先结束原会话再核对。",
		"task_not_found":                         "服务器上的原会话已不存在，本地成果文件仍保留；请安装新版客户端以结束这条提示。",
		"thread_already_running":                 "该会话已有活动或待核对的任务，请先检查任务状态。",
		"edit_history_changed":                   "会话记录已变化，请刷新后重新编辑。",
		"edit_message_not_found":                 "原消息已变化或不存在，请刷新后重新编辑。",
		"runtime_data_must_be_outside_project":   "所选工作目录包含客户端应用数据，请选择独立的项目工作目录。",
		"expert_not_owned":                       "当前专家不属于此账号或已停用，请选择自己的专家。",
		"thread_not_owned":                       "此账号无权访问该专家会话，请重新选择会话。",
		"model_not_available":                    "所选模型当前不可用，请重新选择公司提供的模型。",
		"company_model_failed":                   "公司模型调用失败，请稍后重试；若持续出现请联系管理员检查模型服务。",
		"company_model_timeout":                  "公司模型服务响应超时，请稍后重试；若持续出现请联系管理员检查模型服务。",
		"company_model_connection_lost":          "公司模型服务连接中断，请稍后重试；若持续出现请联系管理员检查模型服务。",
		"company_model_context_overflow":         "公司模型上下文超出限制，请缩短消息或减少附件内容后重试。",
		"company_model_empty_response":           "公司模型服务未返回内容，请稍后重试；若持续出现请联系管理员检查模型服务。",
		"company_model_output_truncated":         "公司模型输出未完整返回，请稍后重试；若持续出现请联系管理员检查模型服务。",
		"company_capability_not_assigned":        "管理员尚未分配此公司能力，请联系管理员。",
		"company_video_generation_not_assigned":  "管理员尚未分配生视频能力，请联系管理员。",
		"company_speech_to_text_not_assigned":    "管理员尚未分配语音识别能力，请联系管理员。",
		"company_text_to_speech_not_assigned":    "管理员尚未分配语音合成能力，请联系管理员。",
		"company_web_search_not_assigned":        "管理员尚未分配联网搜索能力，请联系管理员。",
		"company_video_service_unavailable":      "公司尚未配置可用的生视频服务，请联系管理员。",
		"company_video_download_failed":          "视频已生成，但公司服务下载失败；请联系管理员核对结果，勿重复提交。",
		"company_video_provider_timeout":         "生视频服务响应超时，请联系管理员核对结果；不会自动重复生成。",
		"company_video_provider_unavailable":     "公司生视频服务暂时无法连接，请稍后再试。",
		"company_web_search_service_unavailable": "公司尚未配置可用的联网搜索服务，请联系管理员。",
		"company_web_search_failed":              "公司联网搜索失败，请稍后再试或联系管理员。",
		"company_text_to_speech_failed":          "公司语音合成失败，请联系管理员核对服务配置。",
		"company_image_service_unavailable":      "公司尚未配置可用的生图服务，请联系管理员。",
		"company_image_generation_not_assigned":  "管理员尚未分配生图能力，请联系管理员在员工技能中勾选公司生图。",
		"AGENT_NOT_RUNNING":                      "服务端专家实例尚未启动，请稍后重试或联系管理员检查专家状态。",
		"AGENT_FAILED":                           "服务端专家实例启动失败，请联系管理员检查运行状态。",
		"MODEL_NOT_ASSIGNED":                     "管理员尚未分配此模型，请联系管理员在员工设置中配置可用模型。",
		"employee_resources_changed":             "管理员已更新你的资源权限，请重新发送消息以加载最新配置。",
		"company_image_download_failed":          "图片已生成，但公司服务下载失败；请检查网关图片地址，勿重复提交。",
		"company_image_provider_timeout":         "生图服务响应超时，结果需到网关核对；不会自动重复生成。",
		"company_image_provider_unavailable":     "公司生图服务暂时无法连接，请稍后再试。",
	}
	if message, ok := messages[code]; ok {
		return message
	}
	if strings.HasPrefix(code, "company_model_http_") {
		return "公司模型服务返回错误，请稍后重试；若持续出现请联系管理员检查模型服务。"
	}
	if strings.HasPrefix(code, "company_model_") {
		return "公司模型调用失败，请稍后重试；若持续出现请联系管理员检查模型服务。"
	}
	for _, r := range code {
		if r >= 0x4e00 && r <= 0x9fff {
			return code
		}
	}
	return "本地任务暂不可用，请检查公司连接和客户端工具包（" + code + "）。"
}

// History supplies a receipt, never a blanket permission to open arbitrary paths.
func checkedRuntimeArtifact(workspace, path, digest string, currentReceipt bool) (string, error) {
	root, err := filepath.EvalSymlinks(workspace)
	if err != nil {
		return "", errors.New("本机工作目录不可用")
	}
	if !filepath.IsAbs(path) {
		return "", errors.New("成果文件不在本机工作目录中")
	}
	real, err := filepath.EvalSymlinks(path)
	if err != nil {
		return "", errors.New("成果保存在原电脑或文件已移动，尚未同步到当前目录")
	}
	relative, err := filepath.Rel(root, real)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return "", errors.New("成果保存在原电脑，尚未同步到当前工作目录")
	}
	info, err := os.Stat(real)
	if err != nil || !info.Mode().IsRegular() {
		return "", errors.New("本机成果文件不存在")
	}
	if digest == "" {
		if currentReceipt {
			return real, nil
		}
		return "", errors.New("历史成果缺少校验值，无法确认本机文件")
	}
	expected, err := hex.DecodeString(digest)
	if err != nil || len(expected) != sha256.Size {
		return "", errors.New("成果文件校验值无效")
	}
	f, err := os.Open(real)
	if err != nil {
		return "", errors.New("本机成果文件不可读")
	}
	defer f.Close()
	h := sha256.New()
	if _, err = io.Copy(h, f); err != nil || hex.EncodeToString(h.Sum(nil)) != digest {
		return "", errors.New("本机文件与会话中的成果版本不同，请在工作目录中核对")
	}
	return real, nil
}
