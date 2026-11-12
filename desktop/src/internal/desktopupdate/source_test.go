package desktopupdate

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wailsapp/wails/v3/pkg/updater"
)

func feedFixture(t *testing.T, mutate func(map[string]any), body []byte) (*Source, *httptest.Server) {
	t.Helper()
	digest := sha256.Sum256([]byte("complete executable"))
	artifact := map[string]any{
		"platform": "windows", "arch": "amd64", "filename": "mosswork-company-windows-amd64-0.0.8-update.exe",
		"url":  "../releases/v0.0.8/mosswork-company-windows-amd64-0.0.8-update.exe",
		"size": 19, "digestAlgo": "sha256", "digest": base64.StdEncoding.EncodeToString(digest[:]),
	}
	if mutate != nil {
		mutate(artifact)
	}
	feed := map[string]any{"schemaVersion": 1, "channel": "stable", "version": "0.0.8",
		"metadata": map[string]any{"distribution": "mosswork-company"}, "artifacts": []any{artifact}}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/mosswork/release/latest/desktop-update.json" {
			_ = json.NewEncoder(w).Encode(feed)
			return
		}
		if strings.Contains(r.URL.Path, "/releases/v0.0.8/") {
			_, _ = w.Write(body)
			return
		}
		http.NotFound(w, r)
	}))
	t.Cleanup(server.Close)
	source, err := newSource(server.URL+"/mosswork/release/latest/desktop-update.json", server.Client())
	if err != nil {
		t.Fatal(err)
	}
	return source, server
}

var oldWindows = updater.CheckRequest{CurrentVersion: "0.0.7", Platform: "windows", Arch: "amd64"}

func TestActualManifestDiscoveryAndPinnedDownload(t *testing.T) {
	source, server := feedFixture(t, nil, []byte("complete executable"))
	release, err := source.Check(context.Background(), oldWindows)
	if err != nil || release == nil {
		t.Fatalf("check: %v, %v", release, err)
	}
	if release.Metadata["endpoint.artifact.url"] != server.URL+"/mosswork/release/releases/v0.0.8/"+release.Artifact.Filename {
		t.Fatalf("release did not pin its immutable payload: %v", release.Metadata)
	}
	var downloaded bytes.Buffer
	if err := source.Download(context.Background(), release, &downloaded, nil); err != nil {
		t.Fatal(err)
	}
	if downloaded.String() != "complete executable" {
		t.Fatal("download bytes differ")
	}
	for _, version := range []string{"0.0.8", "0.0.9", "0.1.0"} {
		req := oldWindows
		req.CurrentVersion = version
		if release, err := source.Check(context.Background(), req); err != nil || release != nil {
			t.Fatalf("%s offered a downgrade: %v, %v", version, release, err)
		}
	}
}

func TestRefuseUnsafeOrIncompleteFeed(t *testing.T) {
	for name, change := range map[string]func(map[string]any){
		"missing hash":            func(a map[string]any) { delete(a, "digest") },
		"wrong hash length":       func(a map[string]any) { a["digest"] = "YQ==" },
		"missing size":            func(a map[string]any) { delete(a, "size") },
		"oversized":               func(a map[string]any) { a["size"] = 3 << 30 },
		"installer executable":    func(a map[string]any) { a["filename"] = "mosswork-company-windows-amd64-0.0.8.exe" },
		"path traversal":          func(a map[string]any) { a["filename"] = "../mosswork.exe" },
		"mutable alias":           func(a map[string]any) { a["url"] = "./mosswork-latest-windows-amd64.exe" },
		"foreign origin":          func(a map[string]any) { a["url"] = "https://example.com/mosswork.exe" },
		"wrong version directory": func(a map[string]any) { a["url"] = "../releases/v0.0.7/" + a["filename"].(string) },
		"wrong architecture":      func(a map[string]any) { a["arch"] = "arm64" },
	} {
		t.Run(name, func(t *testing.T) {
			source, _ := feedFixture(t, change, []byte("complete executable"))
			if release, err := source.Check(context.Background(), oldWindows); err == nil || release != nil {
				t.Fatalf("unsafe release accepted: %v %v", release, err)
			}
		})
	}
}

func TestRefuseTruncatedAndOversizedActualDownloads(t *testing.T) {
	for _, body := range []string{"truncated", "complete executable with extra bytes"} {
		source, _ := feedFixture(t, nil, []byte(body))
		release, err := source.Check(context.Background(), oldWindows)
		if err != nil {
			t.Fatal(err)
		}
		if err = source.Download(context.Background(), release, io.Discard, nil); err == nil {
			t.Fatalf("bad size accepted: %q", body)
		}
	}
}

func TestUnsupportedBuildAndPlatformNeverGuessAnArtifact(t *testing.T) {
	source, _ := feedFixture(t, nil, nil)
	for _, req := range []updater.CheckRequest{
		{CurrentVersion: "dev", Platform: "windows", Arch: "amd64"},
		{CurrentVersion: "0.0.7", Platform: "darwin", Arch: "amd64"},
		{CurrentVersion: "0.0.7", Platform: "windows", Arch: "arm64"},
		{CurrentVersion: "0.0.7", Platform: "linux", Arch: "amd64"},
	} {
		if _, err := source.Check(context.Background(), req); err == nil {
			t.Fatalf("unsupported build accepted: %+v", req)
		}
	}
}

// Tests drive the same source on every native CI host while keeping fixture
// architecture independent of the test runner's architecture.
type windowsFixtureSource struct{ *Source }

func (s windowsFixtureSource) Check(ctx context.Context, request updater.CheckRequest) (*updater.Release, error) {
	request.Platform, request.Arch = "windows", "amd64"
	return s.Source.Check(ctx, request)
}

func TestFrameworkRefusesCorruptionBeforeAnyRestart(t *testing.T) {
	source, _ := feedFixture(t, nil, []byte("corrupted executabl"))
	quit := false
	manager, err := New("0.0.7", windowsFixtureSource{source}, func() { quit = true }, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = manager.Check(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err = manager.Download(context.Background()); err == nil {
		t.Fatal("digest mismatch accepted")
	}
	status := manager.Status()
	if status.IsReady || status.ErrorStage != "verify" || quit {
		t.Fatalf("corruption reached install: %+v", status)
	}
}

func TestBusyGuardPreservesVerifiedCandidate(t *testing.T) {
	source, _ := feedFixture(t, nil, []byte("complete executable"))
	quit := false
	manager, err := New("0.0.7", windowsFixtureSource{source}, func() { quit = true }, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = manager.Check(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err = manager.Download(context.Background()); err != nil {
		t.Fatal(err)
	}
	path := manager.engine.DownloadedPath()
	if !strings.HasPrefix(filepath.Base(filepath.Dir(path)), "wails-update-") {
		t.Fatal("unexpected staging directory")
	}
	t.Cleanup(func() { _ = os.RemoveAll(filepath.Dir(path)) })
	if err = manager.Restart(context.Background(), func() error { return errors.New("active work") }); err == nil {
		t.Fatal("restart ignored the active task guard")
	}
	if !manager.Status().IsReady || manager.Status().ErrorStage != "busy" || quit {
		t.Fatalf("busy guard discarded candidate or quit: %+v", manager.Status())
	}
	if err = manager.Check(context.Background()); err != nil || manager.engine.DownloadedPath() != path {
		t.Fatal("second window replaced a ready candidate")
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("verified candidate was removed")
	}
}
