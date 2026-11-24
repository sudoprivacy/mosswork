package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
	"mosswork.desktop/internal/company"
	"mosswork.desktop/internal/companyruntime"
)

func startupTestApp(t *testing.T) *App {
	t.Helper()
	return startupTestAppWithLease(t, time.Now().Add(time.Hour).Unix(), false)
}

func startupTestAppWithLease(t *testing.T, cutoff int64, renewable bool) *App {
	t.Helper()
	t.Setenv("MOSSWORK_HOME", t.TempDir())
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		var hello map[string]any
		if wsjson.Read(r.Context(), conn, &hello) != nil {
			return
		}
		if wsjson.Write(r.Context(), conn, map[string]any{"type": "ready", "device_id": strings.Repeat("b", 32), "owner_user_id": 7, "protocol_version": 2, "heartbeat_seconds": 15, "expires_at": cutoff, "credential": "mosswork-device.test", "lease_supported": renewable}) != nil {
			return
		}
		deadline := cutoff
		for {
			var frame map[string]any
			if wsjson.Read(r.Context(), conn, &frame) != nil {
				return
			}
			if renewable && frame["type"] == "renew" {
				deadline += 120
				if wsjson.Write(r.Context(), conn, map[string]any{"type": "lease_ack", "candidate": frame["candidate"], "expires_at": deadline}) != nil {
					return
				}
			}
		}
	}))
	app := newApp(nil, t.TempDir())
	client := app.company
	t.Cleanup(func() { client.Close(false); server.Close() })
	if err := client.Pair(context.Background(), server.URL, "mosswork-pair.test", "test", runtime.GOOS); err != nil {
		t.Fatal(err)
	}
	app.companyURL, app.workspaceNonce = server.URL, "nonce"
	app.workspacePath, app.workspaceGrant = t.TempDir(), &company.Grant{}
	return app
}

func TestRuntimeWatchdogKeepsWorkerAcrossTwoRenewals(t *testing.T) {
	python := "python3"
	if runtime.GOOS == "windows" {
		python = "python"
	}
	binary, err := exec.LookPath(python)
	if err != nil {
		t.Skip("test worker requires Python")
	}
	cutoff := time.Now().Add(4 * time.Second).Unix()
	app := startupTestAppWithLease(t, cutoff, true)
	script := filepath.Join(t.TempDir(), "worker.py")
	worker := `import hashlib,json,os,sys
digest = ""
for line in sys.stdin:
    req = json.loads(line)
    if req["method"] == "renew_credential":
        digest = hashlib.sha256(req["params"]["credential"].encode()).hexdigest()
    print(json.dumps({"id": req["id"], "result": {"pid": os.getpid(), "digest": digest}}), flush=True)
`
	if err := os.WriteFile(script, []byte(worker), 0600); err != nil {
		t.Fatal(err)
	}
	process, err := companyruntime.Start(binary, script, app.workspacePath, os.Environ(), func(companyruntime.Message) {})
	if err != nil {
		t.Fatal(err)
	}
	defer process.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	read := func() (int, string) {
		raw, err := process.Request(ctx, "status", nil)
		var status struct {
			PID    int
			Digest string
		}
		if err != nil || json.Unmarshal(raw, &status) != nil || status.PID <= 0 {
			t.Fatal("worker not responding", err)
		}
		return status.PID, status.Digest
	}
	pid, _ := read()
	_, initial := app.company.CurrentLease()
	app.companyRuntime = process
	go app.watchRuntimeIdentity("nonce", app.companyURL, app.runtimeEpoch, initial)
	for round := 1; round <= 2; round++ {
		if err := app.company.Renew(ctx, app.companyURL, "mosswork-renew.test"); err != nil {
			t.Fatal(err)
		}
		for {
			status, key := app.company.CurrentLease()
			current, digest := read()
			if current != pid {
				t.Fatal("worker restarted")
			}
			if status.ExpiresAt == cutoff+int64(round)*120 && digest == fmt.Sprintf("%x", sha256.Sum256([]byte(key))) {
				break
			}
			time.Sleep(25 * time.Millisecond)
		}
	}
	time.Sleep(time.Until(time.Unix(cutoff+1, 0)))
	if current, _ := read(); current != pid {
		t.Fatal("worker restarted at the old cutoff")
	}
}

func TestEmitRuntimeProgressWithoutWindowIsSafe(t *testing.T) {
	a := &App{}
	a.emitRuntimeProgress("nonce", "https://company.test", 0, "tools")
}

func TestRuntimePreparationDoesNotBlockImagePreview(t *testing.T) {
	a := startupTestApp(t)
	path, digest := previewFixture(t, a.workspacePath, "image.png", previewPNG(t, 8, 8))
	entered, release, done := make(chan struct{}), make(chan struct{}), make(chan error, 1)
	defer close(release)
	go func() {
		_, err := a.ensureRuntimeProcess("nonce", a.companyURL, "status", func() (string, error) { close(entered); <-release; return "", errors.New("test preparation stopped") })
		done <- err
	}()
	<-entered
	preview := make(chan error, 1)
	params, _ := json.Marshal(map[string]string{"path": path, "sha256": digest})
	go func() {
		_, err := a.performRuntimeAction("nonce", a.companyURL, "preview_image", params)
		preview <- err
	}()
	select {
	case err := <-preview:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("image preview blocked behind runtime verification")
	}
}

func TestRuntimePreparationDiscardsRevokedDirectory(t *testing.T) {
	a := startupTestApp(t)
	entered, release, done := make(chan struct{}), make(chan struct{}), make(chan error, 1)
	base := a.companyURL
	root := t.TempDir()
	go func() {
		_, err := a.ensureRuntimeProcess("nonce", base, "status", func() (string, error) { close(entered); <-release; return root, nil })
		done <- err
	}()
	<-entered
	a.companyMu.Lock()
	a.stopCompanyRuntimeLocked()
	a.workspaceGrant = nil
	a.companyMu.Unlock()
	close(release)
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "启动已取消") {
			t.Fatalf("stale preparation was not cancelled: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("revoked preparation did not finish")
	}
	if a.companyRuntime != nil {
		t.Fatal("revoked account started a worker")
	}
}

func TestRuntimeRepairsCachedToolsBeforeSendingTask(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fixture uses a POSIX shell as the bundled worker")
	}
	a := startupTestApp(t)
	t.Cleanup(func() {
		a.companyMu.Lock()
		defer a.companyMu.Unlock()
		a.stopCompanyRuntimeLocked()
	})
	source := validRuntimeFixture(t)
	worker := `#!/bin/sh
test "$(wc -c < "$MOSSWORK_COMPANY_PACKAGES/octop/company_runtime.py")" -eq 2048 || exit 1
while IFS= read -r request; do
    case "$request" in
        *'"method":"initialize"'*) printf '%s\n' '{"id":"1","result":{}}' ;;
        *'"method":"start"'*)
            printf '%s\n' start >> submissions
            printf '%s\n' '{"id":"2","result":{"task_id":"recovered"}}' ;;
    esac
done
` + strings.Repeat("#\n", 512)
	if err := os.WriteFile(pythonExe(source), []byte(worker), 0755); err != nil {
		t.Fatal(err)
	}
	manifestPath := filepath.Join(source, "company-runtime-manifest.json")
	data, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	var manifest companyManifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		t.Fatal(err)
	}
	relative, _ := filepath.Rel(source, pythonExe(source))
	digest := sha256.Sum256([]byte(worker))
	manifest.Files[filepath.ToSlash(relative)] = hex.EncodeToString(digest[:])
	data, _ = json.Marshal(manifest)
	if err := os.WriteFile(manifestPath, data, 0600); err != nil {
		t.Fatal(err)
	}
	archive := filepath.Join(t.TempDir(), "tools.zip")
	companyToolArchive(t, source, archive)
	t.Setenv("MOSSWORK_DESKTOP_PORTABLE_ZIP", archive)
	root, err := ensureCompanyPortable()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readyCompanyPortable.invalidate(root) })
	dependency := filepath.Join(root, "packages", "octop", "company_runtime.py")
	if err := os.WriteFile(dependency, []byte("damaged"), 0600); err != nil {
		t.Fatal(err)
	}

	result, err := a.performRuntimeAction("nonce", a.companyURL, "start", json.RawMessage(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if reply, ok := result.(json.RawMessage); !ok || string(reply) != `{"task_id":"recovered"}` {
		t.Fatalf("unexpected task reply: %v", result)
	}
	submissions, err := os.ReadFile(filepath.Join(a.workspacePath, "submissions"))
	if err != nil || string(submissions) != "start\n" {
		t.Fatalf("expected a single task submission, got %q (%v)", submissions, err)
	}
	if err := validateCompanyRuntime(root); err != nil {
		t.Fatalf("startup did not restore the bundled files: %v", err)
	}
}

func validRuntimeFixture(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	names := []string{"launch.py", "packages/octop/company_runtime.py", "packages/moss_harness/__init__.py", "packages/moss_memory/__init__.py", "packages/moss_browser/__init__.py", "packages/moss_gateway/__init__.py", "packages/openpyxl/__init__.py", "packages/docx/__init__.py", "packages/pptx/__init__.py", "packages/pypdf/__init__.py", "packages/PIL/__init__.py", "packages/pandas/__init__.py"}
	py, _ := filepath.Rel(root, pythonExe(root))
	names = append(names, filepath.ToSlash(py))
	suffix := ""
	if runtime.GOOS == "windows" {
		suffix = ".exe"
	}
	names = append(names, "tools/ffmpeg"+suffix, "tools/ffprobe"+suffix)
	manifest := companyManifest{Protocol: 1, Platform: runtime.GOOS, Arch: runtime.GOARCH, Files: map[string]string{}}
	for _, name := range names {
		p := filepath.Join(root, name)
		os.MkdirAll(filepath.Dir(p), 0700)
		data := []byte(strings.Repeat("x", 2048))
		if err := os.WriteFile(p, data, 0755); err != nil {
			t.Fatal(err)
		}
		h := sha256.Sum256(data)
		manifest.Files[name] = hex.EncodeToString(h[:])
	}
	data, _ := json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(root, "company-runtime-manifest.json"), data, 0600); err != nil {
		t.Fatal(err)
	}
	return root
}

func TestRuntimeSingleWalkStillChecksAllContentsAndEntries(t *testing.T) {
	for _, kind := range []string{"valid", "changed", "missing", "unexpected", "symlink"} {
		t.Run(kind, func(t *testing.T) {
			root := validRuntimeFixture(t)
			target := filepath.Join(root, "launch.py")
			switch kind {
			case "changed":
				os.WriteFile(target, []byte(strings.Repeat("y", 2048)), 0755)
			case "missing":
				os.Remove(target)
			case "unexpected":
				os.WriteFile(filepath.Join(root, "extra.py"), []byte("x"), 0600)
			case "symlink":
				os.Remove(target)
				if err := os.Symlink(filepath.Join(root, "tools", "ffmpeg"), target); err != nil {
					t.Skip(err)
				}
			}
			err := validateCompanyRuntime(root)
			if (err == nil) != (kind == "valid") {
				t.Fatalf("%s: %v", kind, err)
			}
		})
	}
}

func BenchmarkCompanyRuntimeValidation(b *testing.B) {
	root := os.Getenv("MOSSWORK_BENCH_RUNTIME")
	if root == "" {
		b.Skip("set MOSSWORK_BENCH_RUNTIME to an installed runtime")
	}
	for _, workers := range []int{1, 4} {
		b.Run(fmt.Sprintf("workers-%d", workers), func(b *testing.B) {
			for b.Loop() {
				if err := validateCompanyRuntimeWorkers(root, workers); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}

func TestCompanyPortablePreparationSharedDuringLogin(t *testing.T) {
	root, home := validRuntimeFixture(t), t.TempDir()
	archive := filepath.Join(t.TempDir(), "tools.zip")
	if err := os.WriteFile(archive, []byte("first"), 0600); err != nil {
		t.Fatal(err)
	}
	var cache companyPortableCache
	var calls atomic.Int32
	entered, release := make(chan struct{}), make(chan struct{})
	prepare := func(_, _ string) (string, error) {
		if calls.Add(1) == 1 {
			close(entered)
			<-release
		}
		return root, nil
	}
	results := make(chan error, 8)
	var group sync.WaitGroup
	for range 8 {
		group.Go(func() {
			got, err := cache.prepare(archive, home, prepare)
			if err == nil && got != root {
				err = errors.New("different prepared runtime")
			}
			results <- err
		})
	}
	<-entered
	close(release)
	group.Wait()
	close(results)
	for err := range results {
		if err != nil {
			t.Fatal(err)
		}
	}
	if calls.Load() != 1 {
		t.Fatal("login and worker repeated preparation", calls.Load())
	}
	if err := os.WriteFile(archive, []byte("changed archive"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := cache.prepare(archive, home, prepare); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 2 {
		t.Fatal("changed archive did not invalidate preparation")
	}
}

func TestCompanyPortablePreparationRetriesFailures(t *testing.T) {
	archive := filepath.Join(t.TempDir(), "tools.zip")
	if err := os.WriteFile(archive, []byte("tools"), 0600); err != nil {
		t.Fatal(err)
	}
	root := validRuntimeFixture(t)
	var cache companyPortableCache
	calls := 0
	prepare := func(_, _ string) (string, error) {
		calls++
		if calls == 1 {
			return "", errors.New("interrupted extraction")
		}
		return root, nil
	}
	if _, err := cache.prepare(archive, "home", prepare); err == nil {
		t.Fatal("missing failure")
	}
	for range 2 {
		if _, err := cache.prepare(archive, "home", prepare); err != nil {
			t.Fatal(err)
		}
	}
	if calls != 2 {
		t.Fatal("failed preparation was cached or success repeated", calls)
	}
}
