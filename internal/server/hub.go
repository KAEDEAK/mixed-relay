package server

import (
	"encoding/json"
	"strings"
	"sync"

	"github.com/KAEDEAK/mixed-relay/internal/proto"
)

// hub serializes all state mutation through a single goroutine. Inbound
// frames from sessions are pushed onto inCh; the run loop drains them and
// dispatches via handlers.go.
type hub struct {
	srv *Server

	inCh    chan inMsg
	addCh   chan *Session
	dropCh  chan *Session
	stopCh  chan struct{}
	stopped sync.Once

	// runtime state (only touched on the hub goroutine after Start)
	sessions map[string]*Session // by NICK (lower-cased)
	channels map[string]*chanMembers
	store    *Store

	// status subscriptions: subscriber session id -> set of nicks (or "*")
	statusSubs map[string]map[string]struct{}

	// session id counter — only the listener increments it.
}

type chanMembers struct {
	members map[string]*Session // NICK (lower) -> session
}

type inMsg struct {
	sess *Session
	msg  *proto.Message
}

func newHub(srv *Server, store *Store) *hub {
	return &hub{
		srv:        srv,
		inCh:       make(chan inMsg, 256),
		addCh:      make(chan *Session, 16),
		dropCh:     make(chan *Session, 16),
		stopCh:     make(chan struct{}),
		sessions:   map[string]*Session{},
		channels:   map[string]*chanMembers{},
		store:      store,
		statusSubs: map[string]map[string]struct{}{},
	}
}

func (h *hub) run() {
	consoleSingleton.sys("hub started")
	for {
		select {
		case <-h.stopCh:
			consoleSingleton.sys("hub stopping")
			return
		case s := <-h.addCh:
			// Session is just connected. NICK/USER not yet known.
			_ = s
		case s := <-h.dropCh:
			h.handleDrop(s)
		case in := <-h.inCh:
			h.dispatch(in.sess, in.msg)
		}
	}
}

func (h *hub) stop() {
	h.stopped.Do(func() { close(h.stopCh) })
}

// post is called by session read goroutines.
func (h *hub) post(s *Session, m *proto.Message) {
	select {
	case h.inCh <- inMsg{s, m}:
	case <-h.stopCh:
	}
}

func (h *hub) addSession(s *Session) {
	select {
	case h.addCh <- s:
	case <-h.stopCh:
	}
}

func (h *hub) dropSession(s *Session) {
	select {
	case h.dropCh <- s:
	case <-h.stopCh:
	}
}

// ----- helpers (called only on hub goroutine) -----

func nickKey(n string) string { return strings.ToLower(n) }

func (h *hub) sessionByNick(n string) *Session {
	return h.sessions[nickKey(n)]
}

func (h *hub) joinChan(s *Session, name string) (*chanMembers, bool, error) {
	cm, exists := h.channels[name]
	if !exists {
		// ensure persistence opens too
		if _, err := h.store.channel(name); err != nil {
			return nil, false, err
		}
		cm = &chanMembers{members: map[string]*Session{}}
		h.channels[name] = cm
	}
	first := false
	if _, ok := cm.members[nickKey(s.nick)]; !ok {
		cm.members[nickKey(s.nick)] = s
		s.chans[name] = struct{}{}
		first = true
	}
	return cm, first, nil
}

func (h *hub) partChan(s *Session, name string) bool {
	cm, ok := h.channels[name]
	if !ok {
		return false
	}
	if _, ok := cm.members[nickKey(s.nick)]; !ok {
		return false
	}
	delete(cm.members, nickKey(s.nick))
	delete(s.chans, name)
	return true
}

func (h *hub) broadcast(channel string, line string, except *Session) {
	cm, ok := h.channels[channel]
	if !ok {
		return
	}
	for _, m := range cm.members {
		if m == except {
			continue
		}
		_ = m.SendLine(line)
	}
}

func (h *hub) handleDrop(s *Session) {
	if !s.registered {
		return
	}
	for ch := range s.chans {
		// Persist part
		if cs, err := h.store.channel(ch); err == nil {
			cs.Append("part", s.nick, s.user, "connection closed")
		}
		// Broadcast
		line := proto.New(s.nick+"!"+s.user+"@host", "PART", []string{ch}, "connection closed", true).Encode()
		h.broadcast(ch, line, s)
		if cm, ok := h.channels[ch]; ok {
			delete(cm.members, nickKey(s.nick))
		}
	}
	delete(h.sessions, nickKey(s.nick))
	delete(h.statusSubs, s.id)
	consoleSingleton.sys("session dropped: %s (%s)", s.id, s.nick)
}

// emitStatusUpdate fans out a status update to subscribers (excluding self).
func (h *hub) emitStatusUpdate(s *Session, payload json.RawMessage) {
	line := proto.New(s.nick, "MRSTATUS", []string{s.nick}, string(payload), true).Encode()
	for sid, set := range h.statusSubs {
		if sid == s.id {
			continue
		}
		_, all := set["*"]
		_, named := set[nickKey(s.nick)]
		if !all && !named {
			continue
		}
		// look up the subscriber
		for _, candidate := range h.sessions {
			if candidate.id == sid {
				_ = candidate.SendLine(line)
				break
			}
		}
	}
}
