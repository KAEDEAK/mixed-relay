package server

import (
	"bufio"
	"encoding/json"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/KAEDEAK/mixed-relay/internal/proto"
)

// Liveness parameters (F-11). Tunable here. The server sends PING if the
// session has been silent for idlePingAfter, then drops the session if no
// PONG (or any other frame, which also counts as liveness) comes back
// within pongDeadline. These values are deliberately generous enough to
// cover bridge/tool-call latency while short enough to detect a dead socket
// before the human operator notices.
const (
	idlePingAfter = 30 * time.Second
	pongDeadline  = 15 * time.Second
	keepaliveTick = 5 * time.Second
)

// Session is one TCP connection. State that other goroutines (the hub) need
// to mutate is guarded with atomics or accessed under hub serialization.
type Session struct {
	id     string // n-NN
	conn   net.Conn
	reader *bufio.Reader
	writer *bufio.Writer

	wmu sync.Mutex // serializes writes on this connection only

	// identity (mutated only on the hub goroutine)
	nick    string
	user    string
	kind    string
	profile json.RawMessage // last MRPROFILE SET payload
	status  json.RawMessage // last MRSTATUS SET payload (post-merge)

	registered bool

	// channels this session is currently in (set on the hub goroutine)
	chans map[string]struct{}

	// Liveness state (F-11). lastRecv is updated from the read goroutine on
	// any inbound frame (not just PONG — any proof-of-life counts).
	// pingOutstanding is set when the keepaliver has sent a PING and is
	// waiting for a response. Atomic access only.
	lastRecvUnix    atomic.Int64 // unix-nano; updated on every recv
	pingSentUnix    atomic.Int64 // 0 when no ping outstanding
	keepaliveStop   chan struct{}
	keepaliveOnce   sync.Once

	closed atomic.Bool
}

func newSession(id string, c net.Conn) *Session {
	s := &Session{
		id:            id,
		conn:          c,
		reader:        bufio.NewReaderSize(c, proto.MaxLineBytes+2),
		writer:        bufio.NewWriter(c),
		chans:         map[string]struct{}{},
		keepaliveStop: make(chan struct{}),
	}
	s.lastRecvUnix.Store(time.Now().UnixNano())
	return s
}

// SendLine writes one wire line plus CRLF and echoes it to the console.
// Safe to call from any goroutine.
func (s *Session) SendLine(line string) error {
	if s.closed.Load() {
		return nil
	}
	s.wmu.Lock()
	defer s.wmu.Unlock()
	consoleSingleton.send(s.id, s.nick, line)
	if _, err := s.writer.WriteString(line); err != nil {
		return err
	}
	if _, err := s.writer.WriteString("\r\n"); err != nil {
		return err
	}
	return s.writer.Flush()
}

// Send is a convenience that builds a Message and sends it.
func (s *Session) Send(prefix, cmd string, params []string, trailing string, hasTrail bool) error {
	m := proto.New(prefix, cmd, params, trailing, hasTrail)
	return s.SendLine(m.Encode())
}

// SendErr sends an ERROR frame.
func (s *Session) SendErr(code, text string) error {
	return s.Send("", "ERROR", []string{code}, text, true)
}

// SendNotice sends a server NOTICE to this session.
func (s *Session) SendNotice(text string) error {
	return s.Send(":server", "NOTICE", []string{s.nickOrPlaceholder()}, text, true)
}

func (s *Session) nickOrPlaceholder() string {
	if s.nick != "" {
		return s.nick
	}
	return "*"
}

func (s *Session) close() {
	if s.closed.CompareAndSwap(false, true) {
		s.keepaliveOnce.Do(func() { close(s.keepaliveStop) })
		_ = s.conn.Close()
	}
}

// startKeepalive runs the per-session liveness loop. It is launched once
// immediately after newSession by the accept loop. Behaviour (F-11):
//
//   - Every keepaliveTick, check how long it's been since we heard from
//     this client.
//   - If more than idlePingAfter and no ping is currently outstanding,
//     send a PING with a nonce and record the send time.
//   - If a ping is already outstanding and pongDeadline has elapsed without
//     any inbound frame, close the session. The read loop's EOF will then
//     run the standard drop path (PART broadcast etc.).
func (s *Session) startKeepalive() {
	go func() {
		t := time.NewTicker(keepaliveTick)
		defer t.Stop()
		for {
			select {
			case <-s.keepaliveStop:
				return
			case <-t.C:
				now := time.Now()
				lastRecv := time.Unix(0, s.lastRecvUnix.Load())
				if ps := s.pingSentUnix.Load(); ps > 0 {
					if now.Sub(time.Unix(0, ps)) > pongDeadline {
						consoleSingleton.sys("session %s keepalive: PONG timeout, dropping", s.id)
						s.close()
						return
					}
				}
				if now.Sub(lastRecv) > idlePingAfter && s.pingSentUnix.Load() == 0 {
					nonce := strings.ReplaceAll(now.Format("150405.000000000"), ".", "")
					if err := s.Send(":server", "PING", nil, nonce, true); err != nil {
						s.close()
						return
					}
					s.pingSentUnix.Store(now.UnixNano())
				}
			}
		}
	}()
}

// noteRecv records proof-of-life from the client and clears any pending
// ping. Called from the read goroutine on every inbound frame.
func (s *Session) noteRecv() {
	s.lastRecvUnix.Store(time.Now().UnixNano())
	s.pingSentUnix.Store(0)
}

// readLines reads lines until the connection drops, dispatching each through
// the supplied callback. The callback runs on the read goroutine; the hub
// goroutine is reached via the in-channel inside the callback.
func (s *Session) readLines(onLine func(*Session, *proto.Message)) {
	defer s.close()
	for {
		line, err := s.reader.ReadString('\n')
		if err != nil {
			return
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			continue
		}
		s.noteRecv() // liveness — any line counts, not just PONG
		consoleSingleton.recv(s.id, s.nick, line)
		m, perr := proto.Parse(line)
		if perr != nil {
			_ = s.SendErr("400", "bad syntax: "+perr.Error())
			continue
		}
		// PONG is absorbed inline so it doesn't spam the hub dispatch.
		if m.Command == "PONG" {
			continue
		}
		onLine(s, m)
	}
}
