package deps

import (
	"log/slog"
	"mosswork/communication/internal/config"
	"mosswork/communication/internal/httpx"
	"mosswork/communication/internal/store"
)

type Deps struct {
	Cfg        config.Config
	Log        *slog.Logger
	Store      *store.Store
	Cursor     *httpx.Cursor
	InstanceID string
	Realtime   RealtimeNotifier
}
type RealtimeNotifier interface {
	Wakeup(...string)
	Kick(...string)
	OnlineUsers() []string
}
