package company

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"strings"
	"time"

	"github.com/coder/websocket"
)

func statusFromReady(identity ready) Status {
	status := Status{State: "online", DeviceID: identity.DeviceID, OwnerUserID: identity.OwnerUserID, ExpiresAt: identity.ExpiresAt}
	if identity.LeaseSupported {
		status.RenewAt = renewalTime(identity.ExpiresAt)
	}
	return status
}

func renewalTime(expires int64) int64 {
	return expires - max(1, min(900, (expires-time.Now().Unix())/3))
}

func watchLeaseDeadline(ctx context.Context, cancel context.CancelFunc, deadline func() int64) {
	for {
		until := time.Until(time.Unix(deadline(), 0))
		if until <= 0 {
			cancel()
			return
		}
		timer := time.NewTimer(until)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

func leaseDigest(secret string) string {
	digest := sha256.Sum256([]byte(secret))
	return hex.EncodeToString(digest[:])
}

func newDeviceSecret() string {
	var value [32]byte
	rand.Read(value[:])
	return "mosswork-device." + base64.RawURLEncoding.EncodeToString(value[:])
}

func (c *Client) acceptLease(conn *websocket.Conn, ack frame) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn != conn || c.pendingLease == "" || leaseDigest(c.pendingLease) != ack.Candidate {
		return
	}
	if ack.ExpiresAt > c.status.ExpiresAt {
		c.credential, c.pendingLease = c.pendingLease, ""
		c.status.ExpiresAt, c.status.RenewAt = ack.ExpiresAt, renewalTime(ack.ExpiresAt)
	}
}

func (c *Client) CurrentLease() (Status, string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.status.State != "online" || time.Now().Unix() >= c.status.ExpiresAt {
		return c.status, ""
	}
	return c.status, c.credential
}

func (c *Client) resumeLease(ctx context.Context, base string, handshake hello) (*websocket.Conn, ready, string, error) {
	c.mu.Lock()
	current, candidate := c.credential, c.pendingLease
	c.mu.Unlock()
	if candidate != "" {
		handshake.Credential = candidate
		if next, identity, err := c.connect(ctx, base, handshake); err == nil {
			return next, identity, candidate, nil
		}
	}
	handshake.Credential = current
	next, identity, err := c.connect(ctx, base, handshake)
	return next, identity, current, err
}

func (c *Client) Renew(ctx context.Context, base, ticket string) error {
	var hint string
	if index := strings.LastIndexByte(ticket, '.'); index > len("mosswork-renew.") {
		ticket, hint = ticket[:index], ticket[index+1:]
	}
	c.mu.Lock()
	status, conn, proof := c.status, c.conn, c.credential
	if status.RenewAt == 0 || proof == "" {
		c.mu.Unlock()
		return errors.New("device_renewal_unavailable")
	}
	confirmed := status.State == "disconnected" && c.pendingLease != "" && hint == leaseDigest(ticket + leaseDigest(c.pendingLease))[:24]
	if confirmed {
		proof, c.credential, c.pendingLease = c.pendingLease, c.pendingLease, ""
	}
	if c.pendingLease == "" {
		c.pendingLease = newDeviceSecret()
	}
	candidate, handshake := c.pendingLease, c.deviceHello
	if status.State == "online" && conn != nil && status.ExpiresAt > time.Now().Unix() {
		c.mu.Unlock()
		return writeResult(ctx, conn, &c.writeMu, map[string]any{"type": "renew", "ticket": ticket, "candidate": leaseDigest(candidate)})
	}
	if status.State != "disconnected" || c.cancel != nil || status.ExpiresAt > time.Now().Unix() {
		c.mu.Unlock()
		return errors.New("device_renewal_unavailable")
	}
	c.mu.Unlock()
	handshake.Type, handshake.Credential, handshake.Proof, handshake.Candidate = "recover", ticket, proof, leaseDigest(candidate)
	handshake.Grants = wireGrants(c.files.Grants())
	var next *websocket.Conn
	var identity ready
	var err error
	if confirmed {
		resume := handshake
		resume.Type, resume.Credential, resume.Proof, resume.Candidate = "hello", proof, "", ""
		next, identity, err = c.connect(ctx, base, resume)
		if err == nil {
			candidate = proof
		}
	}
	if next == nil {
		next, identity, err = c.connect(ctx, base, handshake)
	}
	if err != nil {
		handshake.Type, handshake.Credential, handshake.Proof, handshake.Candidate = "hello", candidate, "", ""
		next, identity, err = c.connect(ctx, base, handshake)
	}
	if err != nil {
		return err
	}
	c.mu.Lock()
	if c.status != status || c.credential != proof || !identity.LeaseSupported || identity.DeviceID != status.DeviceID || identity.OwnerUserID != status.OwnerUserID {
		c.mu.Unlock()
		next.CloseNow()
		return errors.New("device_session_changed")
	}
	runCtx, cancel := context.WithCancel(context.Background())
	c.status, c.cancel, c.conn = statusFromReady(identity), cancel, next
	c.credential, c.pendingLease = candidate, ""
	handshake.Type, handshake.Credential, handshake.Proof, handshake.Candidate = "hello", candidate, "", ""
	c.mu.Unlock()
	go c.run(runCtx, base, handshake, next, identity)
	return nil
}
