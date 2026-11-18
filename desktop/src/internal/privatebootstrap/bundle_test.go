package privatebootstrap

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func bundleFixture(t *testing.T) Installer {
	t.Helper()
	root := t.TempDir()
	files := map[string]string{}
	for _, name := range []string{"Start.command", "private_cloud_config.py", "private_macos_entry.py", "bin/scode", "bin/nexusd-cluster", "bin/nexus-agent-enroll", "bin/grpcurl", "python-runtime.tar.gz"} {
		path := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(name), 0600); err != nil {
			t.Fatal(err)
		}
		files[name] = fmt.Sprintf("%x", sha256.Sum256([]byte(name)))
	}
	manifest, _ := json.Marshal(map[string]any{"platform": "macos-arm64", "files": files})
	if err := os.WriteFile(filepath.Join(root, "native-bundle.json"), manifest, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "SHA256SUMS"), []byte("fixture checksum list"), 0600); err != nil {
		t.Fatal(err)
	}
	return Installer{
		Bundle: root, Root: filepath.Join(t.TempDir(), "not-installed"), Platform: "macos-arm64",
		ManifestSHA256:  fmt.Sprintf("%x", sha256.Sum256(manifest)),
		ChecksumsSHA256: fmt.Sprintf("%x", sha256.Sum256([]byte("fixture checksum list"))),
	}
}

func TestNativeBundleCheckRejectsTamperingAndOtherArchitecture(t *testing.T) {
	i := bundleFixture(t)
	if err := i.VerifyBundle("macos-arm64"); err != nil {
		t.Fatal(err)
	}
	if err := i.VerifyBundle("macos-x64"); err == nil {
		t.Fatal("wrong architecture accepted")
	}
	if err := os.WriteFile(filepath.Join(i.Bundle, "bin/scode"), []byte("changed"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := i.VerifyBundle("macos-arm64"); err == nil {
		t.Fatal("modified executable accepted")
	}
	if _, err := os.Stat(i.Root); !os.IsNotExist(err) {
		t.Fatal("integrity check installed a deployment")
	}
}

func TestNativeBundleRejectsRewrittenMetadataAndExtraFiles(t *testing.T) {
	for _, mutation := range []string{"manifest-and-script", "manifest-script-and-checksums", "manifest-whitespace", "checksums", "extra-python", "extra-directory"} {
		t.Run(mutation, func(t *testing.T) {
			i := bundleFixture(t)
			manifestPath := filepath.Join(i.Bundle, "native-bundle.json")
			data, err := os.ReadFile(manifestPath)
			if err != nil {
				t.Fatal(err)
			}
			switch mutation {
			case "manifest-and-script", "manifest-script-and-checksums":
				var manifest map[string]any
				if err := json.Unmarshal(data, &manifest); err != nil {
					t.Fatal(err)
				}
				changed := []byte("replacement configuration code")
				if err := os.WriteFile(filepath.Join(i.Bundle, "private_cloud_config.py"), changed, 0600); err != nil {
					t.Fatal(err)
				}
				manifest["files"].(map[string]any)["private_cloud_config.py"] = fmt.Sprintf("%x", sha256.Sum256(changed))
				data, _ = json.Marshal(manifest)
				err = os.WriteFile(manifestPath, data, 0600)
				if err == nil && mutation == "manifest-script-and-checksums" {
					var checksums strings.Builder
					for name, digest := range manifest["files"].(map[string]any) {
						fmt.Fprintf(&checksums, "%s  %s\n", digest, name)
					}
					fmt.Fprintf(&checksums, "%x  native-bundle.json\n", sha256.Sum256(data))
					err = os.WriteFile(filepath.Join(i.Bundle, "SHA256SUMS"), []byte(checksums.String()), 0600)
				}
			case "manifest-whitespace":
				err = os.WriteFile(manifestPath, append(data, '\n'), 0600)
			case "checksums":
				err = os.WriteFile(filepath.Join(i.Bundle, "SHA256SUMS"), []byte("replacement checksums"), 0600)
			case "extra-python":
				err = os.WriteFile(filepath.Join(i.Bundle, "sitecustomize.py"), []byte("raise SystemExit('injected')"), 0600)
			case "extra-directory":
				err = os.Mkdir(filepath.Join(i.Bundle, "unlisted"), 0700)
			}
			if err != nil {
				t.Fatal(err)
			}
			if err := i.VerifyBundle("macos-arm64"); err == nil {
				t.Fatal("modified bundle accepted")
			}
		})
	}
}

func TestInstallerUsesOriginalEntryAndNoInheritedKeys(t *testing.T) {
	i := bundleFixture(t)
	t.Setenv("ANTHROPIC_API_KEY", "other-model-key")
	t.Setenv("SUDOROUTER_API_KEY", "other-router-key")
	t.Setenv("NEXUS_IDENTITY_DIR", "other-node")
	cmd, err := i.command(context.Background(), "configure-cloud", "--cloud-user", member, "--cloud-organization", org)
	if err != nil {
		t.Fatal(err)
	}
	if cmd.Args[1] != filepath.Join(i.Bundle, "Start.command") || cmd.Args[2] != "configure-cloud" {
		t.Fatal(cmd.Args)
	}
	env := strings.Join(cmd.Env, "\n")
	if strings.Contains(env, "other-") || !strings.Contains(env, "SUDO_PRIVATE_HOME="+i.Root) {
		t.Fatal("wrong child environment")
	}
}

func TestUnpinnedBundleAndRedirectedMetadataAreRefused(t *testing.T) {
	i := bundleFixture(t)
	i.ManifestSHA256 = ""
	if err := i.VerifyBundle(i.Platform); err == nil {
		t.Fatal("missing build pin accepted")
	}
	i = bundleFixture(t)
	path := filepath.Join(i.Bundle, "native-bundle.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "manifest.json")
	if err := os.WriteFile(outside, data, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, path); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	if err := i.VerifyBundle(i.Platform); err == nil {
		t.Fatal("redirected metadata accepted")
	}
}

func TestExecutionPathsRecheckBundleBeforeUsingCredentialsOrStartingProcesses(t *testing.T) {
	i := bundleFixture(t)
	config := filepath.Join(i.Root, "config")
	if err := os.MkdirAll(config, 0700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"settings.json", "sudocode.json"} {
		if err := os.WriteFile(filepath.Join(config, name), []byte("{}"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	who, _ := json.Marshal(map[string]string{"userId": member, "organizationId": org})
	if err := os.WriteFile(filepath.Join(config, "cloud-account.json"), who, 0600); err != nil {
		t.Fatal(err)
	}
	if err := i.VerifyBundle(i.Platform); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(i.Bundle, "private_cloud_config.py"), []byte("changed after initial verification"), 0600); err != nil {
		t.Fatal(err)
	}
	for name, action := range map[string]func() error{
		"configure": func() error {
			return i.Configure(context.Background(), Identity{MemberID: member, OrganizationID: org}, []byte("secret-projection-canary"))
		},
		"prepare": func() error { return i.Prepare(context.Background()) },
		"open":    i.Open,
	} {
		if err := action(); err == nil || !strings.Contains(err.Error(), "checksum") {
			t.Fatalf("%s did not stop at integrity check: %v", name, err)
		}
	}
	if _, err := os.Stat(filepath.Join(i.Root, "Open Assistant.command")); !os.IsNotExist(err) {
		t.Fatal("launcher was written for modified payload")
	}
}

func TestTerminalLauncherVerifiesBeforeStartingTheOriginalEntry(t *testing.T) {
	i := Installer{Bundle: "/Applications/O'Brien.app/Contents/Resources/private-bootstrap", Root: "/Users/a b/private", Verifier: "/Applications/O'Brien.app/Contents/MacOS/mosswork"}
	launcher := i.launcherContent()
	guard := shellQuote(i.Verifier) + " --verify-private-payload >/dev/null || exit $?\n"
	if !strings.HasPrefix(launcher, "#!/bin/bash\n"+guard) {
		t.Fatal(launcher)
	}
	if !strings.Contains(launcher, "exec /bin/bash "+shellQuote(filepath.Join(i.Bundle, "Start.command"))) {
		t.Fatal(launcher)
	}
}

func TestLauncherQuotesSpacesAndQuotesWithoutExpandingShell(t *testing.T) {
	if got := shellQuote("/Users/O'Brien/a $literal"); got != "'/Users/O'\\''Brien/a $literal'" {
		t.Fatal(got)
	}
}

func TestUnconfiguredInstallationCannotOpenAssistant(t *testing.T) {
	i := Installer{Bundle: t.TempDir(), Root: t.TempDir()}
	if i.Configured() {
		t.Fatal("empty root configured")
	}
	if err := i.Open(); err == nil {
		t.Fatal("opened unconfigured assistant")
	}
}
