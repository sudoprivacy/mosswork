// Package desktopupdate owns the native client update channel and lifecycle.
// Server and Python package updates are independent of this channel.
package desktopupdate

import (
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/wailsapp/wails/v3/pkg/updater"
	"github.com/wailsapp/wails/v3/pkg/updater/providers/endpoint"
)

const FeedURL = "https://sudowork-release-1309794936.cos.accelerate.myqcloud.com/mosswork/release/latest/desktop-update.json"

var stableVersion = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)

//go:embed targets.json
var targetJSON []byte

type targetSpec struct {
	Platform         string `json:"platform"`
	Arch             string `json:"arch"`
	ArtifactPlatform string `json:"artifact_platform"`
	Extension        string `json:"extension"`
}

// The release manifest generator reads this same checked-in target inventory.
var targets = func() []targetSpec {
	var result []targetSpec
	if err := json.Unmarshal(targetJSON, &result); err != nil {
		panic(err)
	}
	return result
}()

// Source adds the application's mandatory integrity and whole-payload policy
// to Wails' standard manifest provider. Discovery and download stay pinned to
// one immutable release, even if latest advances during a download.
type Source struct {
	provider updater.Provider
	origin   *url.URL
}

func NewSource() (*Source, error) {
	return newSource(FeedURL, &http.Client{Timeout: 20 * time.Minute})
}

func newSource(feed string, client *http.Client) (*Source, error) {
	origin, err := url.Parse(feed)
	if err != nil || origin.Host == "" {
		return nil, errors.New("invalid desktop update feed")
	}
	clientCopy := *client
	clientCopy.CheckRedirect = func(request *http.Request, via []*http.Request) error {
		if len(via) >= 5 || request.URL.Scheme != origin.Scheme || request.URL.Host != origin.Host || request.URL.User != nil {
			return errors.New("desktop update redirect leaves the trusted distribution origin")
		}
		return nil
	}
	provider, err := endpoint.New(endpoint.Config{URL: feed, Channel: "stable", HTTPClient: &clientCopy,
		Headers: map[string]string{"Cache-Control": "no-cache"}})
	if err != nil {
		return nil, err
	}
	return &Source{provider: provider, origin: origin}, nil
}

func (s *Source) Name() string { return "mosswork-stable" }

func (s *Source) Check(ctx context.Context, req updater.CheckRequest) (*updater.Release, error) {
	if !stableVersion.MatchString(req.CurrentVersion) {
		return nil, errors.New("automatic updates require a stable company desktop build")
	}
	if !Supported(req.Platform, req.Arch) {
		return nil, errors.New("no native company desktop update for this architecture")
	}
	release, err := s.provider.Check(ctx, req)
	if err != nil || release == nil {
		return release, err
	}
	if err := s.validate(release); err != nil {
		return nil, err
	}
	if release.Artifact.Platform != req.Platform || release.Artifact.Arch != req.Arch {
		return nil, errors.New("desktop update architecture mismatch")
	}
	return release, nil
}

func targetFor(platform, arch string) (targetSpec, bool) {
	for _, target := range targets {
		if target.Platform == platform && target.Arch == arch {
			return target, true
		}
	}
	return targetSpec{}, false
}

// Supported reports targets actually shipped by this distribution.
func Supported(platform, arch string) bool {
	_, ok := targetFor(platform, arch)
	return ok
}

func (s *Source) validate(release *updater.Release) error {
	if release == nil || !stableVersion.MatchString(release.Version) || release.Channel != "stable" {
		return errors.New("desktop update must identify a stable release")
	}
	if release.Metadata["distribution"] != "mosswork-company" {
		return errors.New("desktop update distribution mismatch")
	}
	artifact := release.Artifact
	target, ok := targetFor(artifact.Platform, artifact.Arch)
	if !ok || artifact.Size <= 0 || artifact.Size > 2<<30 {
		return errors.New("invalid native desktop update artifact")
	}
	expected := fmt.Sprintf("mosswork-company-%s-%s-%s-update.%s", target.ArtifactPlatform, artifact.Arch, release.Version, target.Extension)
	if artifact.Filename != expected {
		return errors.New("update must contain the complete native client; installers are not executable updates")
	}
	verification := release.Verification
	if verification == nil || verification.DigestAlgo != "sha256" || len(verification.Digest) != sha256.Size {
		return errors.New("desktop update requires a SHA-256 digest")
	}
	raw, _ := release.Metadata["endpoint.artifact.url"].(string)
	location, err := url.Parse(raw)
	expectedPath := path.Join(path.Dir(path.Dir(s.origin.Path)), "releases", "v"+release.Version, expected)
	if err != nil || location.Scheme != s.origin.Scheme || location.Host != s.origin.Host ||
		location.User != nil || location.RawQuery != "" || location.Fragment != "" ||
		location.Path != expectedPath || location.RawPath != "" {
		return errors.New("desktop update must use this feed's immutable release directory")
	}
	return nil
}

func (s *Source) Download(ctx context.Context, release *updater.Release, dst io.Writer, progress func(int64, int64)) error {
	if err := s.validate(release); err != nil {
		return err
	}
	// The framework hashes the actual stream. Bound the stream independently
	// of Content-Length and require its size to match the selected manifest.
	writer := &boundedWriter{dst: dst, remaining: release.Artifact.Size}
	if err := s.provider.Download(ctx, release, writer, progress); err != nil {
		return err
	}
	if writer.remaining != 0 {
		return errors.New("desktop update download is incomplete")
	}
	return nil
}

type boundedWriter struct {
	dst       io.Writer
	remaining int64
}

func (w *boundedWriter) Write(data []byte) (int, error) {
	if int64(len(data)) > w.remaining {
		return 0, errors.New("desktop update exceeds its declared size")
	}
	n, err := w.dst.Write(data)
	w.remaining -= int64(n)
	return n, err
}

// Target identifies the same application unit as the framework's swap helper.
func Target(executable, platform string) string {
	if platform == "darwin" {
		for current := executable; current != filepath.Dir(current); current = filepath.Dir(current) {
			if strings.HasSuffix(current, ".app") {
				return current
			}
		}
	}
	return executable
}
