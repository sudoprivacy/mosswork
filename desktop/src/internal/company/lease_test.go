package company

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

func TestDeviceLeaseKeepsPendingWordWriteAcrossCutoff(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()
	work := t.TempDir()
	client := NewClient(t.TempDir())
	callCh := make(chan frame, 1)
	receipt := make(chan error, 1)
	expires := time.Now().Add(3 * time.Second).Unix()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		run := func() error {
			var handshake hello
			if err := wsjson.Read(ctx, conn, &handshake); err != nil {
				return err
			}
			identity := ready{Type: "ready", DeviceID: strings.Repeat("b", 32), OwnerUserID: 7, ProtocolVersion: 2, HeartbeatSeconds: 15, ExpiresAt: expires, Credential: newDeviceSecret(), LeaseSupported: true}
			if err := wsjson.Write(ctx, conn, identity); err != nil {
				return err
			}
			var message map[string]any
			if err := wsjson.Read(ctx, conn, &message); err != nil || message["type"] != "lease_enable" {
				return errors.New("missing negotiation")
			}
			if err := wsjson.Read(ctx, conn, &message); err != nil || message["type"] != "grants" {
				return errors.New("missing grants")
			}
			var call frame
			select {
			case call = <-callCh:
			case <-ctx.Done():
				return ctx.Err()
			}
			if err := wsjson.Write(ctx, conn, call); err != nil {
				return err
			}
			if err := wsjson.Read(ctx, conn, &message); err != nil || message["type"] != "renew" {
				return errors.New("missing renewal")
			}
			if err := wsjson.Write(ctx, conn, map[string]any{"type": "lease_ack", "candidate": message["candidate"], "expires_at": expires + 120}); err != nil {
				return err
			}
			var result struct {
				CallID string     `json:"call_id"`
				Result FileResult `json:"result"`
			}
			if err := wsjson.Read(ctx, conn, &result); err != nil {
				return err
			}
			if result.CallID != call.CallID || result.Result.Status != "completed" {
				return errors.New("original operation did not complete")
			}
			return nil
		}
		receipt <- run()
		<-ctx.Done()
	}))
	defer server.Close()
	defer cancel()
	defer client.Close(false)
	if err := client.Pair(ctx, server.URL, "mosswork-pair.test", "PC", "windows"); err != nil {
		t.Fatal(err)
	}
	grant, err := client.AddGrant(ctx, work, true, true)
	if err != nil {
		t.Fatal(err)
	}
	call := fileCall(grant, "local_write_file", "report.docx", "跨期文档")
	call.Type = "call"
	client.files.mu.Lock()
	callCh <- call
	err = client.Renew(ctx, server.URL, "mosswork-renew.test")
	if err == nil {
		time.Sleep(time.Until(time.Unix(expires, 0)) + 100*time.Millisecond)
	}
	client.files.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	if err := <-receipt; err != nil {
		t.Fatal(err)
	}
	if status := client.Status(); status.State != "online" || status.ExpiresAt != expires+120 {
		t.Fatalf("lease: %+v", status)
	}
	data, err := os.ReadFile(filepath.Join(work, "report.docx"))
	if err != nil {
		t.Fatal(err)
	}
	text, err := readDocument("report.docx", data)
	if err != nil || !strings.Contains(text, "跨期文档") {
		t.Fatal("invalid Word file", err)
	}
	if entries, err := os.ReadDir(work); err != nil || len(entries) != 1 {
		t.Fatal("unexpected workspace output", err)
	}
}

func TestExpiredLeaseRecoveryAndLostConfirmation(t *testing.T) {
	for _, confirmed := range []bool{false, true} {
		name := "expired"
		if confirmed {
			name = "committed_without_ack"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			client := NewClient(t.TempDir())
			old := newDeviceSecret()
			pending := newDeviceSecret()
			client.status = Status{State: "disconnected", DeviceID: "native", OwnerUserID: 7, ExpiresAt: time.Now().Unix() - 1, RenewAt: 1}
			client.credential, client.pendingLease = old, pending
			client.deviceHello = hello{Name: "PC", Platform: "windows", ProtocolVersion: 2}
			ticket := "mosswork-renew." + strings.Repeat("a", 43)
			current := old
			if confirmed {
				current = pending
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := websocket.Accept(w, r, nil)
				if err != nil {
					return
				}
				defer conn.CloseNow()
				var handshake hello
				if err := wsjson.Read(ctx, conn, &handshake); err != nil {
					return
				}
				if confirmed && (handshake.Type != "hello" || handshake.Credential != pending) {
					t.Error("did not confirm committed key")
					return
				}
				if !confirmed && (handshake.Type != "recover" || handshake.Credential != ticket || handshake.Proof != old || handshake.Candidate != leaseDigest(pending)) {
					t.Error("invalid recovery proof")
					return
				}
				if err := wsjson.Write(ctx, conn, ready{Type: "ready", DeviceID: "native", OwnerUserID: 7, ProtocolVersion: 2, HeartbeatSeconds: 15, ExpiresAt: time.Now().Unix() + 120, LeaseSupported: true}); err != nil {
					return
				}
				<-ctx.Done()
			}))
			defer server.Close()
			defer cancel()
			defer client.Close(false)
			if err := client.Renew(ctx, server.URL, ticket+"."+leaseDigest(ticket + leaseDigest(current))[:24]); err != nil {
				t.Fatal(err)
			}
			status, key := client.CurrentLease()
			if status.DeviceID != "native" || key != pending || status.State != "online" {
				t.Fatal("original device not recovered")
			}
		})
	}
}
