package server

// Concept guard tests: wire-level verification that every anti-feature listed
// in docs/requirements_v003.md §2.2 stays rejected, and that the load-bearing
// transparency/persistence promises (F-7.5, F-8, F-9.5/9.6, A-4, A-6, A-7)
// keep working. These tests exist so future refactors can't silently drift
// back into the v0.0.2 task-tool shape.

import (
	"context"
	"strings"
	"testing"
)

// ----- §2.2 anti-feature rejection -----

// TestMRTaskFamilyAllRejected — every legacy task verb must come back as
// 421 unknown command. Covers MRTASK NEW / ACCEPT / DECLINE / PROGRESS /
// DONE / FAIL / CANCEL / HANDOVER.
func TestMRTaskFamilyAllRejected(t *testing.T) {
	srv := startServer(t)
	c := dial(t, srv.Addr())
	defer c.close()
	register(t, c, "alice", "alice")
	verbs := []string{
		"MRTASK NEW bob :do it",
		"MRTASK ACCEPT t-1",
		"MRTASK DECLINE t-1 :no",
		"MRTASK PROGRESS t-1 :50%",
		"MRTASK DONE t-1 :ok",
		"MRTASK FAIL t-1 :nope",
		"MRTASK CANCEL t-1 :bye",
		"MRTASK HANDOVER t-1 carol",
	}
	for _, v := range verbs {
		c.send(t, v)
		line := c.recvUntil(t, "ERROR 421", 5)
		if !strings.Contains(line, "unknown command") {
			t.Fatalf("verb %q: expected 421 unknown command, got %s", v, line)
		}
	}
}

// TestMRCapsLegacyRejected — MRCAPS was a rev6 legacy command; v003 has no
// capability separate from MRPROFILE. It must be unknown.
func TestMRCapsLegacyRejected(t *testing.T) {
	srv := startServer(t)
	c := dial(t, srv.Addr())
	defer c.close()
	register(t, c, "alice", "alice")
	c.send(t, "MRCAPS GET alice")
	c.recvUntil(t, "ERROR 421", 5)
}

// TestDMAndNoticeRejected — F-2.4 / F-2.5. Already covered for a single
// verb elsewhere; this variant asserts both the PRIVMSG and NOTICE paths at
// once to pin the pair.
func TestDMAndNoticeRejected(t *testing.T) {
	srv := startServer(t)
	a := dial(t, srv.Addr())
	defer a.close()
	b := dial(t, srv.Addr())
	defer b.close()
	register(t, a, "alice", "alice")
	register(t, b, "bob", "bob")
	a.send(t, "PRIVMSG bob :secret")
	l := a.recvUntil(t, "ERROR 403", 5)
	if !strings.Contains(l, "direct messages disabled") {
		t.Fatalf("DM reject text missing: %s", l)
	}
	a.send(t, "NOTICE bob :shhh")
	l = a.recvUntil(t, "ERROR 403", 5)
	if !strings.Contains(l, "NOTICE") {
		t.Fatalf("NOTICE reject text missing: %s", l)
	}
}

// ----- F-7.5 / A-6 — cursor continuity across NICK rename -----

// TestCursorSurvivesRename — cursor is keyed by (USER, channel). Renaming
// NICK must not orphan the cursor.
func TestCursorSurvivesRename(t *testing.T) {
	srv := startServer(t)
	c := dial(t, srv.Addr())
	defer c.close()
	register(t, c, "alice", "alice-key")
	c.send(t, "JOIN #ren")
	c.recvUntil(t, "MRWELCOME #ren", 5)
	for i := 0; i < 3; i++ {
		c.send(t, "PRIVMSG #ren :one")
		c.recvUntil(t, "PRIVMSG #ren", 5)
	}
	c.send(t, "MRREAD SET #ren 3")
	c.recvUntil(t, "MRREAD #ren 3", 5)
	// Rename NICK. USER ("alice-key") is unchanged, so the cursor must
	// still be findable under the same key.
	c.send(t, "NICK alice2")
	c.recvUntil(t, "NICK", 5)
	c.send(t, "MRREAD GET #ren")
	got := c.recvUntil(t, "MRREAD #ren", 5)
	if !strings.Contains(got, " 3") {
		t.Fatalf("cursor lost after rename: %s", got)
	}
}

// ----- A-4 — server restart preserves channel log and cursors -----

// TestPersistenceAcrossRestart — stop the Server, spin up a new one on the
// same data dir, reconnect, and verify the old entries come back via welcome
// bundle summary and MRHISTORY.
func TestPersistenceAcrossRestart(t *testing.T) {
	dir := t.TempDir()

	// --- first life ---
	srv1 := New("127.0.0.1:0", dir)
	if err := srv1.Start(context.Background()); err != nil {
		t.Fatalf("start1: %v", err)
	}
	addr1 := srv1.Addr()
	c := dial(t, addr1)
	register(t, c, "alice", "alice")
	c.send(t, "JOIN #keep")
	c.recvUntil(t, "MRWELCOME #keep", 5)
	c.send(t, "PRIVMSG #keep :before-restart")
	c.recvUntil(t, "PRIVMSG #keep :before-restart", 5)
	c.send(t, "MRREAD SET #keep 99")
	c.recvUntil(t, "MRREAD #keep", 5)
	c.close()
	srv1.Stop()

	// --- second life: new server, same dir ---
	srv2 := New("127.0.0.1:0", dir)
	if err := srv2.Start(context.Background()); err != nil {
		t.Fatalf("start2: %v", err)
	}
	defer srv2.Stop()
	c2 := dial(t, srv2.Addr())
	defer c2.close()
	register(t, c2, "alice", "alice")
	c2.send(t, "JOIN #keep")
	wel := c2.recvUntil(t, "MRWELCOME #keep", 5)
	// last_seq should be >= the msg's seq, last_read_seq should be whatever
	// was set (capped at last_seq).
	if !strings.Contains(wel, `"last_seq"`) {
		t.Fatalf("welcome bundle missing last_seq: %s", wel)
	}
	if strings.Contains(wel, `"last_seq":0`) {
		t.Fatalf("persistence lost — last_seq is 0: %s", wel)
	}
	// Fetch the old message via MRHISTORY BEFORE.
	c2.send(t, "MRHISTORY #keep BEFORE 0 100")
	saw := false
	for i := 0; i < 30; i++ {
		line, err := c2.r.ReadString('\n')
		if err != nil {
			t.Fatalf("read: %v", err)
		}
		if strings.Contains(line, "before-restart") {
			saw = true
		}
		if strings.Contains(line, "MRHISTORY END") {
			break
		}
	}
	if !saw {
		t.Fatalf("did not see persisted message after restart")
	}
}

// ----- A-7 — MRHISTORY transparently spans the archive boundary -----

// TestHistorySpansArchiveBoundary — write N messages, archive them all, then
// verify that MRHISTORY BEFORE still returns them and MRHISTORY AFTER 0 too.
func TestHistorySpansArchiveBoundary(t *testing.T) {
	srv := startServer(t)
	c := dial(t, srv.Addr())
	defer c.close()
	register(t, c, "alice", "alice")
	c.send(t, "JOIN #span")
	c.recvUntil(t, "MRWELCOME #span", 5)
	for i := 0; i < 5; i++ {
		c.send(t, "PRIVMSG #span :msg")
		c.recvUntil(t, "PRIVMSG #span", 5)
	}
	// Archive everything (cutoff far in the future).
	c.send(t, "MRARCHIVE #span BEFORE 9999999999999")
	c.recvUntil(t, "MRARCHIVE #span DONE", 5)

	// After archive, MRHISTORY AFTER 0 must still return the old entries
	// from archive, not an empty result.
	c.send(t, "MRHISTORY #span AFTER 0 100")
	count := 0
	for i := 0; i < 30; i++ {
		line, err := c.r.ReadString('\n')
		if err != nil {
			t.Fatalf("read: %v", err)
		}
		if strings.Contains(line, `"kind":"msg"`) && strings.Contains(line, `"text":"msg"`) {
			count++
		}
		if strings.Contains(line, "MRHISTORY END") {
			break
		}
	}
	if count < 5 {
		t.Fatalf("MRHISTORY did not span archive boundary, saw %d msg entries", count)
	}
}

// ----- F-8 — welcome bundle must be summary-only (no bodies) -----

// ----- feedback-1 must-1 — MRHISTORY n overflow → 413 -----

func TestHistoryNOverflow413(t *testing.T) {
	srv := startServer(t)
	c := dial(t, srv.Addr())
	defer c.close()
	register(t, c, "alice", "alice")
	c.send(t, "JOIN #probe")
	c.recvUntil(t, "MRWELCOME #probe", 5)
	c.send(t, "MRHISTORY #probe AFTER 0 1001")
	line := c.recvUntil(t, "ERROR 413", 5)
	if !strings.Contains(line, "too large") {
		t.Fatalf("expected 413 too large, got %s", line)
	}
}

// ----- feedback-1 must-2 — bad int args → 400 -----

func TestHistoryBadAnchor400(t *testing.T) {
	srv := startServer(t)
	c := dial(t, srv.Addr())
	defer c.close()
	register(t, c, "alice", "alice")
	c.send(t, "JOIN #probe")
	c.recvUntil(t, "MRWELCOME #probe", 5)
	c.send(t, "MRHISTORY #probe AFTER nope 10")
	c.recvUntil(t, "ERROR 400", 5)
}

func TestHistoryBadN400(t *testing.T) {
	srv := startServer(t)
	c := dial(t, srv.Addr())
	defer c.close()
	register(t, c, "alice", "alice")
	c.send(t, "JOIN #probe")
	c.recvUntil(t, "MRWELCOME #probe", 5)
	c.send(t, "MRHISTORY #probe AFTER 0 nope")
	c.recvUntil(t, "ERROR 400", 5)
}

func TestReadSetBadSeq400(t *testing.T) {
	srv := startServer(t)
	c := dial(t, srv.Addr())
	defer c.close()
	register(t, c, "alice", "alice")
	c.send(t, "JOIN #probe")
	c.recvUntil(t, "MRWELCOME #probe", 5)
	c.send(t, "MRREAD SET #probe nope")
	c.recvUntil(t, "ERROR 400", 5)
}

// TestWelcomeBundleSummaryOnly — the welcome body must NOT embed recent
// message text. Clients must fetch unread via MRHISTORY AFTER.
func TestWelcomeBundleSummaryOnly(t *testing.T) {
	srv := startServer(t)
	a := dial(t, srv.Addr())
	register(t, a, "alice", "alice")
	a.send(t, "JOIN #sum")
	a.recvUntil(t, "MRWELCOME #sum", 5)
	a.send(t, "PRIVMSG #sum :unique-body-zxcvb")
	a.recvUntil(t, "PRIVMSG #sum", 5)
	a.close()

	// Fresh connection (same USER) joins — its welcome must report the
	// unread count but must NOT carry the message text inline.
	b := dial(t, srv.Addr())
	defer b.close()
	register(t, b, "alice2", "alice") // same USER → same cursor
	b.send(t, "JOIN #sum")
	wel := b.recvUntil(t, "MRWELCOME #sum", 5)
	if strings.Contains(wel, "unique-body-zxcvb") {
		t.Fatalf("welcome bundle leaked message body (should be summary-only): %s", wel)
	}
	if !strings.Contains(wel, `"unread_count"`) {
		t.Fatalf("welcome bundle missing unread_count: %s", wel)
	}
}
