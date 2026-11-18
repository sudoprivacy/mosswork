package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestBrowseThreadDirectory(t *testing.T) {
	grant, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	const project = "thr_first"
	for _, path := range []string{"thr_first/generated", "thr_second"} {
		if err := os.MkdirAll(filepath.Join(grant, path), 0700); err != nil {
			t.Fatal(err)
		}
	}
	const file = "雨停以后.txt"
	if err := os.WriteFile(filepath.Join(grant, project, "generated", file), []byte("rain"), 0600); err != nil {
		t.Fatal(err)
	}
	listing, _, _, err := browseThreadDirectory(grant, project, "generated", false)
	if err != nil || listing.Root != filepath.Join(grant, project) || len(listing.Entries) != 1 || listing.Entries[0].Name != file {
		t.Fatalf("listing=%+v err=%v", listing, err)
	}
	for _, path := range []string{"../thr_second", "/thr_second", "C:/elsewhere", "generated/" + file} {
		if _, _, _, err := browseThreadDirectory(grant, project, path, false); err == nil {
			t.Fatalf("listed %q", path)
		}
	}
	if _, target, isDir, err := browseThreadDirectory(grant, project, "generated/"+file, true); err != nil || isDir || target != filepath.Join(grant, project, "generated", file) {
		t.Fatalf("target=%q err=%v", target, err)
	}
	if err := os.Symlink(filepath.Join(grant, "thr_second"), filepath.Join(grant, project, "alias")); err != nil {
		t.Skip("symlink unavailable")
	}
	if _, _, _, err := browseThreadDirectory(grant, project, "alias", true); err == nil {
		t.Fatal("opened another project through symlink")
	}
}
