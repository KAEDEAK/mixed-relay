package server

import (
	"bufio"
	"context"
	"net"
	"strings"
	"testing"
	"time"
)

// testClient is a tiny line client for the integration tests.
type testClient struct {
	conn net.Conn
	r    *bufio.Reader
}

func dial(t *testing.T, addr string) *testClient {
	t.Helper()
	c, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	return &testClient{conn: c, r: bufio.NewReader(c)}
}

func (c *testClient) send(t *testing.T, line string) {
	t.Helper()
	if _, err := c.conn.Write([]byte(line + "\r\n")); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func (c *testClient) recvUntil(t *testing.T, prefix string, max int) string {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for i := 0; i < max; i++ {
		_ = c.conn.SetReadDeadline(deadline)
		line, err := c.r.ReadString('\n')
		if err != nil {
			t.Fatalf("recvUntil(%q): %v", prefix, err)
		}
		line = strings.TrimRight(line, "\r\n")
		if strings.Contains(line, prefix) {
			return line
		}
	}
	t.Fatalf("recvUntil(%q): not found within %d lines", prefix, max)
	return ""
}

func (c *testClient) close() { c.conn.Close() }

func startServer(t *testing.T) *Server {
	t.Helper()
	dir := t.TempDir()
	srv := New("127.0.0.1:0", dir)
	if err := srv.Start(context.Background()); err != nil {
		t.Fatalf("start: %v", err)
	}
	t.Cleanup(srv.Stop)
	return srv
}

func register(t *testing.T, c *testClient, nick, user string) {
	t.Helper()
	c.send(t, "NICK "+nick)
	c.send(t, "USER "+user+" 0 * :"+nick)
	c.send(t, "MRKIND agent")
	c.recvUntil(t, "MRWELCOME", 5)
}

func TestRegisterAndJoin(t *testing.T) {
	srv := startServer(t)
	c := dial(t, srv.Addr())
	defer c.close()
	register(t, c, "alice", "alice")
	c.send(t, "JOIN #lobby")
	line := c.recvUntil(t, "MRWELCOME #lobby", 5)
	if !strings.Contains(line, `"channel":"#lobby"`) {
		t.Fatalf("welcome bundle missing channel: %s", line)
	}
	if !strings.Contains(line, `"last_seq"`) || !strings.Contains(line, `"last_read_seq"`) {
		t.Fatalf("welcome bundle missing cursor fields: %s", line)
	}
}

func TestPrivmsgChannelOnly(t *testing.T) {
	srv := startServer(t)
	a := dial(t, srv.Addr())
	defer a.close()
	b := dial(t, srv.Addr())
	defer b.close()
	register(t, a, "alice", "alice")
	register(t, b, "bob", "bob")
	a.send(t, "JOIN #room")
	a.recvUntil(t, "MRWELCOME #room", 5)
	b.send(t, "JOIN #room")
	b.recvUntil(t, "MRWELCOME #room", 5)
	// Drain alice's view of bob's join.
	a.recvUntil(t, "JOIN #room", 5)
	a.send(t, "PRIVMSG #room :hello bob")
	got := b.recvUntil(t, "PRIVMSG #room :hello bob", 5)
	if !strings.Contains(got, "alice") {
		t.Fatalf("expected alice prefix, got %s", got)
	}
}

func TestDMRejected(t *testing.T) {
	srv := startServer(t)
	a := dial(t, srv.Addr())
	defer a.close()
	b := dial(t, srv.Addr())
	defer b.close()
	register(t, a, "alice", "alice")
	register(t, b, "bob", "bob")
	a.send(t, "PRIVMSG bob :secret")
	line := a.recvUntil(t, "ERROR 403", 5)
	if !strings.Contains(line, "direct messages disabled") {
		t.Fatalf("expected DM rejection text, got %s", line)
	}
}

func TestNoticeFromClientRejected(t *testing.T) {
	srv := startServer(t)
	a := dial(t, srv.Addr())
	defer a.close()
	register(t, a, "alice", "alice")
	a.send(t, "NOTICE bob :hi")
	a.recvUntil(t, "ERROR 403", 5)
}

func TestMRTaskGone(t *testing.T) {
	srv := startServer(t)
	a := dial(t, srv.Addr())
	defer a.close()
	register(t, a, "alice", "alice")
	a.send(t, "MRTASK NEW bob :do something")
	a.recvUntil(t, "ERROR 421", 5)
}

func TestHistoryAfterAndCursor(t *testing.T) {
	srv := startServer(t)
	a := dial(t, srv.Addr())
	defer a.close()
	register(t, a, "alice", "alice")
	a.send(t, "JOIN #log")
	a.recvUntil(t, "MRWELCOME #log", 5)
	for i := 0; i < 3; i++ {
		a.send(t, "PRIVMSG #log :line")
		a.recvUntil(t, "PRIVMSG #log", 5)
	}
	a.send(t, "MRHISTORY #log AFTER 0 100")
	end := a.recvUntil(t, "MRHISTORY END", 20)
	if !strings.Contains(end, "#log") {
		t.Fatalf("history end mismatched: %s", end)
	}
	a.send(t, "MRREAD SET #log 1000")
	a.recvUntil(t, "MRREAD #log", 5)
	a.send(t, "MRREAD GET #log")
	got := a.recvUntil(t, "MRREAD #log", 5)
	if !strings.Contains(got, "#log") {
		t.Fatalf("read get mismatched: %s", got)
	}
}

func TestArchive(t *testing.T) {
	srv := startServer(t)
	a := dial(t, srv.Addr())
	defer a.close()
	register(t, a, "alice", "alice")
	a.send(t, "JOIN #arch")
	a.recvUntil(t, "MRWELCOME #arch", 5)
	a.send(t, "PRIVMSG #arch :one")
	a.recvUntil(t, "PRIVMSG #arch :one", 5)
	// Cutoff far in the future to capture everything.
	a.send(t, "MRARCHIVE #arch BEFORE 9999999999999")
	a.recvUntil(t, "MRARCHIVE #arch DONE", 5)
	a.send(t, "MRARCHIVE LIST #arch")
	a.recvUntil(t, "MRARCHIVE #arch END", 10)
}

// TestPurgeBroadcastsToOthersOnly — F-13.6 contract: when a member runs
// MRPURGE, the channel announce goes to the OTHER members, while the caller
// receives only the DONE reply. This shape preserves the pre-existing
// "send MRPURGE, wait for DONE" synchronous pattern for clients that already
// exist outside this repo, while still satisfying transparency (T4) for
// observers in the channel.
func TestPurgeBroadcastsToOthersOnly(t *testing.T) {
	srv := startServer(t)
	a := dial(t, srv.Addr())
	defer a.close()
	b := dial(t, srv.Addr())
	defer b.close()
	register(t, a, "alice", "alice")
	register(t, b, "bob", "bob")
	a.send(t, "JOIN #pg")
	a.recvUntil(t, "MRWELCOME #pg", 5)
	b.send(t, "JOIN #pg")
	b.recvUntil(t, "MRWELCOME #pg", 5)
	// Drain alice's view of bob's JOIN broadcast.
	a.recvUntil(t, "JOIN #pg", 5)
	// Lay down a message so there's something to purge.
	a.send(t, "PRIVMSG #pg :ping")
	a.recvUntil(t, "PRIVMSG #pg :ping", 5)
	b.recvUntil(t, "PRIVMSG #pg :ping", 5)

	// Alice triggers the purge. She must see ONLY the DONE reply (no echoed
	// broadcast back to the caller — that would break existing synchronous
	// clients that read until DONE).
	a.send(t, "MRPURGE #pg")
	first := a.recvUntil(t, "MRPURGE", 5)
	if !strings.Contains(first, "DONE") {
		t.Fatalf("caller's first MRPURGE frame must be DONE reply, got: %s", first)
	}

	// Bob (other channel member) must see the broadcast frame and it must
	// NOT contain "DONE" — it's the prefix-tagged informational form.
	bcast := b.recvUntil(t, "MRPURGE", 5)
	if !strings.Contains(bcast, "alice!alice@host MRPURGE #pg") {
		t.Fatalf("bob did not see broadcast purge frame: %s", bcast)
	}
	if strings.Contains(bcast, "DONE") {
		t.Fatalf("bob received the caller-only DONE frame: %s", bcast)
	}
}
