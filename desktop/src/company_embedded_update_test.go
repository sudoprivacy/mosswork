package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func embeddedCompanyFixture(t *testing.T, marker string, version ...string) []byte {
	t.Helper()
	root := validRuntimeFixture(t)
	launch := []byte(marker)
	if err := os.WriteFile(filepath.Join(root, "launch.py"), launch, 0600); err != nil {
		t.Fatal(err)
	}
	manifestPath := filepath.Join(root, "company-runtime-manifest.json")
	data, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	var manifest companyManifest
	if err = json.Unmarshal(data, &manifest); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(launch)
	manifest.Files["launch.py"] = hex.EncodeToString(digest[:])
	data, _ = json.Marshal(manifest)
	if len(version) > 0 {
		var document map[string]any
		if err := json.Unmarshal(data, &document); err != nil {
			t.Fatal(err)
		}
		document["mosswork_version"] = version[0]
		data, _ = json.Marshal(document)
	}
	if err = os.WriteFile(manifestPath, data, 0600); err != nil {
		t.Fatal(err)
	}
	archive := filepath.Join(t.TempDir(), "company.zip")
	companyToolArchive(t, root, archive)
	data, err = os.ReadFile(archive)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestEmbeddedCompanyUpdateSelectsMatchingWholeRuntimeAndPreservesOldData(t *testing.T) {
	t.Setenv("MOSSWORK_HOME", t.TempDir())
	t.Setenv("MOSSWORK_DESKTOP_PORTABLE_ZIP", "")
	previous := embeddedPortable
	t.Cleanup(func() { embeddedPortable = previous })
	embeddedPortable = embeddedCompanyFixture(t, "old runtime")
	old, err := ensureCompanyPortable()
	if err != nil {
		t.Fatal(err)
	}
	sentinel := filepath.Join(mossworkHome(), "account-settings.json")
	if err = os.WriteFile(sentinel, []byte("keep account data"), 0600); err != nil {
		t.Fatal(err)
	}
	embeddedPortable = embeddedCompanyFixture(t, "new runtime")
	current, err := ensureCompanyPortable()
	if err != nil {
		t.Fatal(err)
	}
	if old == current {
		t.Fatal("updated shell selected the old runtime")
	}
	for root, want := range map[string]string{old: "old runtime", current: "new runtime"} {
		data, err := os.ReadFile(filepath.Join(root, "launch.py"))
		if err != nil || string(data) != want {
			t.Fatalf("runtime mismatch: %s, %s, %v", root, data, err)
		}
	}
	if data, err := os.ReadFile(sentinel); err != nil || string(data) != "keep account data" {
		t.Fatal("runtime update touched account data")
	}
	embeddedPortable = []byte("corrupt runtime")
	if _, err = ensureCompanyPortable(); err == nil {
		t.Fatal("corrupt embedded runtime was accepted")
	}
	if data, err := os.ReadFile(filepath.Join(current, "launch.py")); err != nil || string(data) != "new runtime" {
		t.Fatal("failed next runtime corrupted the qualified installation")
	}
}
