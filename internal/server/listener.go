package server

import (
	"context"
	"fmt"
	"net"
	"sync/atomic"
)

// Server is the top-level MixedRelay server.
type Server struct {
	addr     string
	dataDir  string
	listener net.Listener
	hub      *hub
	store    *Store
	idCount  atomic.Int64
}

// New constructs a Server. Call Start to begin accepting.
func New(addr, dataDir string) *Server {
	return &Server{addr: addr, dataDir: dataDir}
}

// Start binds the listener and launches the hub goroutine. Returns once the
// server is ready to accept; subsequent accept loop runs in the background.
func (s *Server) Start(ctx context.Context) error {
	store, err := NewStore(s.dataDir)
	if err != nil {
		return fmt.Errorf("store: %w", err)
	}
	s.store = store
	s.hub = newHub(s, store)
	go s.hub.run()

	ln, err := net.Listen("tcp", s.addr)
	if err != nil {
		return fmt.Errorf("listen %s: %w", s.addr, err)
	}
	s.listener = ln
	consoleSingleton.sys("listening on %s, data=%s", ln.Addr(), s.dataDir)
	go s.acceptLoop(ctx)
	return nil
}

// Addr returns the bound address (useful when addr was ":0").
func (s *Server) Addr() string {
	if s.listener == nil {
		return ""
	}
	return s.listener.Addr().String()
}

// Stop closes the listener, hub, and store.
func (s *Server) Stop() {
	if s.listener != nil {
		_ = s.listener.Close()
	}
	if s.hub != nil {
		s.hub.stop()
	}
	if s.store != nil {
		s.store.Close()
	}
}

func (s *Server) acceptLoop(ctx context.Context) {
	for {
		conn, err := s.listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			// listener closed
			return
		}
		id := fmt.Sprintf("n-%d", s.idCount.Add(1))
		sess := newSession(id, conn)
		consoleSingleton.sys("accepted %s from %s", id, conn.RemoteAddr())
		s.hub.addSession(sess)
		sess.startKeepalive()
		go func() {
			sess.readLines(s.hub.post)
			s.hub.dropSession(sess)
		}()
	}
}
