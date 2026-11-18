package privatebootstrap

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Installer invokes Stack's owned entry, never reimplements Node lifecycle.
// Bundle and Root are chosen by the trusted desktop, not web-page arguments.
type Installer struct {
	Bundle, Root, Platform, Verifier string
	ManifestSHA256, ChecksumsSHA256  string
}

func (i Installer) VerifyBundle(platform string) error {
	if _, err := i.environment(); err != nil {
		return err
	}
	base, err := filepath.EvalSymlinks(i.Bundle)
	if err != nil {
		return err
	}
	// The desktop build supplies these pins independently of the mutable payload.
	// Whole-application authenticity still belongs to platform code signing.
	if len(i.ManifestSHA256) != 64 || len(i.ChecksumsSHA256) != 64 {
		return errors.New("Private payload has no build-time integrity pins")
	}
	metadata := map[string]string{"native-bundle.json": i.ManifestSHA256, "SHA256SUMS": i.ChecksumsSHA256}
	for name, expected := range metadata {
		if err := verifyMember(filepath.Join(base, name), expected); err != nil {
			return fmt.Errorf("Private payload %s: %w", name, err)
		}
	}
	data, err := os.ReadFile(filepath.Join(base, "native-bundle.json"))
	if err != nil || fmt.Sprintf("%x", sha256.Sum256(data)) != i.ManifestSHA256 {
		return errors.New("Private payload manifest changed while verifying")
	}
	var manifest struct {
		Platform string            `json:"platform"`
		Files    map[string]string `json:"files"`
	}
	if json.Unmarshal(data, &manifest) != nil {
		return errors.New("Private payload manifest is invalid")
	}
	if platform == "" || manifest.Platform != platform {
		return errors.New("Private payload does not match this Mac")
	}
	for _, name := range []string{"Start.command", "private_cloud_config.py", "private_macos_entry.py", "bin/scode", "bin/nexusd-cluster", "bin/nexus-agent-enroll", "bin/grpcurl", "python-runtime.tar.gz"} {
		if manifest.Files[name] == "" {
			return errors.New("Private payload is incomplete")
		}
	}
	allowed := map[string]bool{"native-bundle.json": true, "SHA256SUMS": true}
	directories := map[string]bool{".": true}
	for name, expected := range manifest.Files {
		if !fs.ValidPath(name) || name == "." || !filepath.IsLocal(name) || strings.Contains(name, "\\") || metadata[name] != "" {
			return errors.New("Invalid Private payload path")
		}
		allowed[name] = true
		for parent := filepath.Dir(filepath.FromSlash(name)); parent != "."; parent = filepath.Dir(parent) {
			directories[filepath.ToSlash(parent)] = true
		}
		path := filepath.Join(base, filepath.FromSlash(name))
		resolved, err := filepath.EvalSymlinks(path)
		if err != nil || resolved != path {
			return errors.New("Private payload is redirected")
		}
		if err := verifyMember(path, expected); err != nil {
			return fmt.Errorf("Private payload %s: %w", name, err)
		}
	}
	return filepath.WalkDir(base, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(base, path)
		if err != nil {
			return err
		}
		name := filepath.ToSlash(relative)
		if entry.IsDir() && directories[name] {
			return nil
		}
		if !entry.Type().IsRegular() || !allowed[name] {
			return errors.New("Private payload contains an unlisted or redirected member")
		}
		return nil
	})
}

func verifyMember(path, expected string) error {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		return errors.New("member is missing or is not a regular file")
	}
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return err
	}
	if fmt.Sprintf("%x", hash.Sum(nil)) != expected {
		return errors.New("checksum differs from the desktop build")
	}
	return nil
}

func (i Installer) environment() ([]string, error) {
	if !filepath.IsAbs(i.Bundle) || !filepath.IsAbs(i.Root) {
		return nil, errors.New("Private installation paths must be absolute")
	}
	for _, path := range []string{i.Bundle, filepath.Join(i.Bundle, "Start.command")} {
		info, err := os.Lstat(path)
		if err != nil || info.Mode()&os.ModeSymlink != 0 {
			return nil, errors.New("Private installation payload is unavailable")
		}
	}
	// Preserve desktop HOME for its explicitly owned Nexus identity. Never
	// inherit unrelated model credentials or runtime roots into this child.
	var env []string
	for _, name := range []string{"HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_CTYPE"} {
		if value, ok := os.LookupEnv(name); ok {
			env = append(env, name+"="+value)
		}
	}
	return append(env, "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "SUDO_PRIVATE_HOME="+i.Root), nil
}

func (i Installer) command(ctx context.Context, arguments ...string) (*exec.Cmd, error) {
	if err := i.VerifyBundle(i.Platform); err != nil {
		return nil, err
	}
	env, err := i.environment()
	if err != nil {
		return nil, err
	}
	cmd := exec.CommandContext(ctx, "/bin/bash", append([]string{filepath.Join(i.Bundle, "Start.command")}, arguments...)...)
	cmd.Dir = i.Bundle
	cmd.Env = env
	return cmd, nil
}

func (i Installer) Configure(ctx context.Context, who Identity, projection []byte) error {
	cmd, err := i.command(ctx, "configure-cloud", "--cloud-user", who.MemberID, "--cloud-organization", who.OrganizationID)
	if err != nil {
		return err
	}
	cmd.Stdin = bytes.NewReader(projection)
	// Stack's output contains status only; suppress it here because this is a
	// credential-bearing boundary and error details may come from dependencies.
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	if cmd.Run() != nil {
		return errors.New("Cloud login succeeded, but local model setup stopped; the existing configuration was preserved")
	}
	return nil
}

func (i Installer) Prepare(ctx context.Context) error {
	cmd, err := i.command(ctx, "prepare")
	if err != nil {
		return err
	}
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	if cmd.Run() != nil {
		return errors.New("Private preparation stopped; open the installation diagnostics before trying again")
	}
	return nil
}

func (i Installer) Configured() bool {
	// This is a local UX state, not a current server entitlement assertion.
	for _, name := range []string{"cloud-account.json", "settings.json", "sudocode.json"} {
		info, err := os.Lstat(filepath.Join(i.Root, "config", name))
		if err != nil || !info.Mode().IsRegular() {
			return false
		}
	}
	data, err := os.ReadFile(filepath.Join(i.Root, "config/cloud-account.json"))
	var who struct {
		UserID string `json:"userId"`
		OrgID  string `json:"organizationId"`
	}
	return err == nil && json.Unmarshal(data, &who) == nil && identityPattern.MatchString(who.UserID) && identityPattern.MatchString(who.OrgID)
}

func shellQuote(value string) string { return "'" + strings.ReplaceAll(value, "'", "'\\''") + "'" }

func (i Installer) Open() error {
	if !i.Configured() {
		return errors.New("Sign in before opening the bootstrap Agent")
	}
	if err := i.VerifyBundle(i.Platform); err != nil {
		return err
	}
	if !filepath.IsAbs(i.Verifier) {
		return errors.New("Private payload verifier is unavailable")
	}
	// A visible Terminal provides the original interactive SudoCode receive loop.
	// No model key or human token is written into the launcher or its arguments.
	launcher := filepath.Join(i.Root, "Open Assistant.command")
	info, err := os.Lstat(i.Root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("Private installation root is unavailable")
	}
	content := i.launcherContent()
	file, err := os.CreateTemp(i.Root, ".open-assistant-")
	if err != nil {
		return err
	}
	name := file.Name()
	defer os.Remove(name)
	if err = file.Chmod(0700); err == nil {
		_, err = file.WriteString(content)
	}
	if err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if err = os.Rename(name, launcher); err != nil {
		return err
	}
	return exec.Command("/usr/bin/open", "-a", "Terminal", launcher).Run()
}

func (i Installer) launcherContent() string {
	// Verify again when Terminal actually opens this file, including later reuse.
	return "#!/bin/bash\n" + shellQuote(i.Verifier) + " --verify-private-payload >/dev/null || exit $?\nexport SUDO_PRIVATE_HOME=" + shellQuote(i.Root) + "\nexec /bin/bash " + shellQuote(filepath.Join(i.Bundle, "Start.command")) + "\n"
}
