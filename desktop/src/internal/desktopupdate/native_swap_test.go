package desktopupdate

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/wailsapp/wails/v3/pkg/updater"
)

func TestMain(m *testing.M) {
	// Exercise the real framework helper before any test/application startup,
	// exactly as the production main does.
	updater.HandleHelperMode()
	if os.Getenv("MOSSWORK_UPDATER_NATIVE_CHILD") == "1" {
		if err := nativeSwapChild(); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		os.Exit(3) // Restart must have called the normal shutdown callback.
	}
	os.Exit(m.Run())
}

type nativeFixtureProvider struct{ address, filename string }

func (p nativeFixtureProvider) Name() string { return "native-roundtrip-fixture" }
func (p nativeFixtureProvider) Check(ctx context.Context, _ updater.CheckRequest) (*updater.Release, error) {
	request, _ := http.NewRequestWithContext(ctx, http.MethodGet, p.address, nil)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	hash := sha256.New()
	size, err := io.Copy(hash, response.Body)
	if err != nil {
		return nil, err
	}
	return &updater.Release{Version: "0.0.8", Artifact: updater.Artifact{Filename: p.filename, Size: size},
		Verification: &updater.Verification{DigestAlgo: "sha256", Digest: hash.Sum(nil)}}, nil
}
func (p nativeFixtureProvider) Download(ctx context.Context, _ *updater.Release, out io.Writer, progress func(int64, int64)) error {
	request, _ := http.NewRequestWithContext(ctx, http.MethodGet, p.address, nil)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	n, err := io.Copy(out, response.Body)
	progress(n, n)
	return err
}

func nativeSwapChild() error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	manager, err := New("0.0.7", nativeFixtureProvider{os.Getenv("MOSSWORK_UPDATER_NATIVE_URL"), os.Getenv("MOSSWORK_UPDATER_NATIVE_FILENAME")}, func() { os.Exit(0) }, nil)
	if err != nil {
		return err
	}
	if err = manager.Check(ctx); err != nil {
		return err
	}
	if err = manager.Download(ctx); err != nil {
		return err
	}
	return manager.Restart(ctx, func() error { return nil })
}

func TestActualNativeHelperSwapsWholePayloadAndRelaunches(t *testing.T) {
	root := t.TempDir()
	receipt := filepath.Join(root, "relaunched.json")
	program := filepath.Join(root, "new-main.go")
	// The fixture is a real, compiled native executable. Windows models its
	// embedded runtime identity; macOS additionally reads the new Resources file.
	code := fmt.Sprintf(`package main
import("os";"path/filepath")
func main(){
 runtimeVersion:="0.0.8"
 if %t { executable,_:=os.Executable(); data,err:=os.ReadFile(filepath.Join(filepath.Dir(executable),"../Resources/runtime-version.txt"));if err!=nil{panic(err)};runtimeVersion=string(data) }
 if runtimeVersion!="0.0.8" { panic("shell/runtime mismatch") }
 if os.Getenv("WAILS_UPDATER_HELPER")!="" { panic("helper mode leaked into relaunched client") }
 if err:=os.WriteFile(%q,[]byte("shell=0.0.8 runtime="+runtimeVersion),0600);err!=nil{panic(err)}
}
`, runtime.GOOS == "darwin", receipt)
	if err := os.WriteFile(program, []byte(code), 0600); err != nil {
		t.Fatal(err)
	}
	newBinary := filepath.Join(root, "new-mosswork")
	if runtime.GOOS == "windows" {
		newBinary += ".exe"
	}
	build := exec.Command("go", "build", "-o", newBinary, program)
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("compile native fixture: %v\n%s", err, output)
	}
	newBytes, err := os.ReadFile(newBinary)
	if err != nil {
		t.Fatal(err)
	}
	oldBinary := filepath.Join(root, "installed client", "mosswork")
	filename := "new-mosswork"
	payload := newBytes
	if runtime.GOOS == "windows" {
		oldBinary += ".exe"
		filename += ".exe"
	}
	if runtime.GOOS == "darwin" {
		oldApp := filepath.Join(root, "installed client", "mosswork.app")
		newApp := filepath.Join(root, "new app", "mosswork.app")
		oldBinary = filepath.Join(oldApp, "Contents/MacOS/mosswork")
		for _, app := range []string{oldApp, newApp} {
			if err := os.MkdirAll(filepath.Join(app, "Contents/MacOS"), 0755); err != nil {
				t.Fatal(err)
			}
			if err := os.MkdirAll(filepath.Join(app, "Contents/Resources"), 0755); err != nil {
				t.Fatal(err)
			}
			plist := `<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleExecutable</key><string>mosswork</string><key>CFBundleIdentifier</key><string>io.mosswork.update.fixture</string><key>CFBundleVersion</key><string>0.0.8</string><key>LSBackgroundOnly</key><true/></dict></plist>`
			if err := os.WriteFile(filepath.Join(app, "Contents/Info.plist"), []byte(plist), 0600); err != nil {
				t.Fatal(err)
			}
		}
		if err := os.WriteFile(filepath.Join(oldApp, "Contents/Resources/runtime-version.txt"), []byte("0.0.7"), 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(newApp, "Contents/Resources/runtime-version.txt"), []byte("0.0.8"), 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(newApp, "Contents/MacOS/mosswork"), newBytes, 0755); err != nil {
			t.Fatal(err)
		}
		if output, err := exec.Command("codesign", "--force", "--sign", "-", newApp).CombinedOutput(); err != nil {
			t.Fatalf("sign native fixture: %v\n%s", err, output)
		}
		// Signing changes Mach-O bytes; compare the exact signed payload.
		newBytes, err = os.ReadFile(filepath.Join(newApp, "Contents/MacOS/mosswork"))
		if err != nil {
			t.Fatal(err)
		}
		var archive bytes.Buffer
		writer := zip.NewWriter(&archive)
		err = filepath.Walk(newApp, func(path string, info os.FileInfo, err error) error {
			if err != nil {
				return err
			}
			relative, _ := filepath.Rel(filepath.Dir(newApp), path)
			header, err := zip.FileInfoHeader(info)
			if err != nil {
				return err
			}
			header.Name = filepath.ToSlash(relative)
			if info.IsDir() {
				header.Name += "/"
			}
			entry, err := writer.CreateHeader(header)
			if err != nil || info.IsDir() {
				return err
			}
			file, err := os.Open(path)
			if err != nil {
				return err
			}
			defer file.Close()
			_, err = io.Copy(entry, file)
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
		if err = writer.Close(); err != nil {
			t.Fatal(err)
		}
		filename, payload = "new-mosswork.zip", archive.Bytes()
	}
	if err := os.MkdirAll(filepath.Dir(oldBinary), 0755); err != nil {
		t.Fatal(err)
	}
	self, _ := os.Executable()
	oldBytes, err := os.ReadFile(self)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(oldBinary, oldBytes, 0755); err != nil {
		t.Fatal(err)
	}
	sentinel := filepath.Join(root, "user working folder.txt")
	if err = os.WriteFile(sentinel, []byte("keep projects and account data"), 0600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(payload) }))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	child := exec.CommandContext(ctx, oldBinary)
	child.Env = append(os.Environ(), "MOSSWORK_UPDATER_NATIVE_CHILD=1", "MOSSWORK_UPDATER_NATIVE_URL="+server.URL, "MOSSWORK_UPDATER_NATIVE_FILENAME="+filename)
	output, err := child.CombinedOutput()
	if err != nil {
		t.Fatalf("native update child: %v\n%s", err, output)
	}
	deadline := time.Now().Add(30 * time.Second)
	for {
		if data, err := os.ReadFile(receipt); err == nil {
			if string(data) != "shell=0.0.8 runtime=0.0.8" {
				t.Fatalf("wrong relaunched client: %s", data)
			}
			break
		}
		if time.Now().After(deadline) {
			log, _ := os.ReadFile(filepath.Join(os.TempDir(), fmt.Sprintf("wails-update-%d.log", child.Process.Pid)))
			t.Fatalf("new client did not relaunch; helper log:\n%s", log)
		}
		time.Sleep(25 * time.Millisecond)
	}
	actual, err := os.ReadFile(oldBinary)
	if err != nil || !bytes.Equal(actual, newBytes) {
		t.Fatal("installed executable is not the verified new binary")
	}
	if data, err := os.ReadFile(sentinel); err != nil || !strings.Contains(string(data), "keep projects") {
		t.Fatal("update touched user data")
	}
}
