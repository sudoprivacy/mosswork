package desktopupdate

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/wailsapp/wails/v3/pkg/updater"
)

const ServerFeedPath = "/api/update/desktop/latest/desktop-update.json"

// ServerSource resolves the selected server at check time and pins downloads
// to that release's server even if the account/server selection later changes.
type ServerSource struct{ server func() string }

func NewServerSource(server func() string) *ServerSource { return &ServerSource{server: server} }
func (s *ServerSource) Name() string                     { return "mosswork-company-server" }

func serverFeed(base string) (string, error) {
	u, err := url.Parse(base)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" || u.User != nil ||
		u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return "", errors.New("select a company server before checking for client updates")
	}
	return strings.TrimRight(base, "/") + ServerFeedPath, nil
}

func (s *ServerSource) Check(ctx context.Context, req updater.CheckRequest) (*updater.Release, error) {
	feed, err := serverFeed(s.server())
	if err != nil {
		return nil, err
	}
	source, err := newSource(feed, &http.Client{Timeout: 20 * time.Minute})
	if err != nil {
		return nil, err
	}
	release, err := source.Check(ctx, req)
	if release != nil && err == nil {
		release.Metadata["mosswork.server.feed"] = feed
	}
	return release, err
}

func (s *ServerSource) Download(ctx context.Context, release *updater.Release, dst io.Writer, progress func(int64, int64)) error {
	if release == nil {
		return errors.New("no selected client release")
	}
	feed, _ := release.Metadata["mosswork.server.feed"].(string)
	u, err := url.Parse(feed)
	if err != nil || u.Path != ServerFeedPath {
		return errors.New("invalid pinned server release")
	}
	base := *u
	base.Path = ""
	if _, err = serverFeed(base.String()); err != nil {
		return err
	}
	source, err := newSource(feed, &http.Client{Timeout: 20 * time.Minute})
	if err != nil {
		return err
	}
	return source.Download(ctx, release, dst, progress)
}
