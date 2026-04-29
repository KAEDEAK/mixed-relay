package server

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/KAEDEAK/mixed-relay/internal/proto"
)

// dispatch routes one parsed message to a handler. Runs on the hub goroutine.
func (h *hub) dispatch(s *Session, m *proto.Message) {
	// Pre-registration: only NICK / USER / MRKIND / MRPROFILE / PING / QUIT
	// allowed. PING passes through so the liveness loop works before the
	// client has fully registered.
	if !s.registered {
		switch m.Command {
		case "NICK", "USER", "MRKIND", "MRPROFILE", "PING", "QUIT":
			// allowed
		default:
			_ = s.SendErr("451", "you have not registered")
			return
		}
	}
	switch m.Command {
	case "NICK":
		h.cmdNick(s, m)
	case "USER":
		h.cmdUser(s, m)
	case "MRKIND":
		h.cmdMRKind(s, m)
	case "MRPROFILE":
		h.cmdMRProfile(s, m)
	case "JOIN":
		h.cmdJoin(s, m)
	case "PART":
		h.cmdPart(s, m)
	case "PRIVMSG":
		h.cmdPrivmsg(s, m)
	case "NOTICE":
		// Anti-feature §2.2: client-originated NOTICE is forbidden.
		_ = s.SendErr("403", "client NOTICE disabled")
	case "TOPIC":
		h.cmdTopic(s, m)
	case "MRWHO":
		h.cmdMRWho(s, m)
	case "MRCHANNELS":
		h.cmdMRChannels(s, m)
	case "WHOIS":
		h.cmdWhois(s, m)
	case "MRSTATUS":
		h.cmdMRStatus(s, m)
	case "MRSUB":
		h.cmdMRSub(s, m)
	case "MRHISTORY":
		h.cmdMRHistory(s, m)
	case "MRREAD":
		h.cmdMRRead(s, m)
	case "MRARCHIVE":
		h.cmdMRArchive(s, m)
	case "MRPURGE":
		h.cmdMRPurge(s, m)
	case "PING":
		_ = s.Send(":server", "PONG", nil, joinTrail(m), m.HasTrail)
	case "QUIT":
		s.close()
	default:
		_ = s.SendErr("421", "unknown command: "+m.Command)
	}
}

func joinTrail(m *proto.Message) string {
	if m.HasTrail {
		return m.Trailing
	}
	if len(m.Params) > 0 {
		return strings.Join(m.Params, " ")
	}
	return ""
}

// ----- registration -----

func (h *hub) cmdNick(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "NICK requires a nick")
		return
	}
	newNick := m.Params[0]
	if !validNick(newNick) {
		_ = s.SendErr("432", "invalid nick")
		return
	}
	if existing := h.sessionByNick(newNick); existing != nil && existing != s {
		_ = s.SendErr("433", "nick in use")
		return
	}
	old := s.nick
	if old != "" {
		delete(h.sessions, nickKey(old))
	}
	s.nick = newNick
	h.sessions[nickKey(newNick)] = s
	if old == "" {
		// pre-registration NICK; finalize when USER also arrives.
		return
	}
	// Post-rename broadcast.
	line := proto.New(old+"!"+s.user+"@host", "NICK", nil, newNick, true).Encode()
	for ch := range s.chans {
		if cs, err := h.store.channel(ch); err == nil {
			cs.Append("nick", newNick, s.user, "renamed from "+old)
		}
		h.broadcast(ch, line, nil)
	}
}

func validNick(n string) bool {
	if n == "" || len(n) > 32 {
		return false
	}
	for _, r := range n {
		switch {
		case r >= 'a' && r <= 'z':
		case r >= 'A' && r <= 'Z':
		case r >= '0' && r <= '9':
		case r == '-' || r == '_':
		default:
			return false
		}
	}
	return true
}

func (h *hub) cmdUser(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "USER requires a reader key")
		return
	}
	s.user = m.Params[0]
	h.tryFinalizeRegistration(s)
}

func (h *hub) cmdMRKind(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "MRKIND requires a kind")
		return
	}
	k := strings.ToLower(m.Params[0])
	switch k {
	case "human", "agent", "tool", "observer":
	default:
		_ = s.SendErr("400", "unknown kind")
		return
	}
	s.kind = k
}

func (h *hub) cmdMRProfile(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "MRPROFILE requires SET or GET")
		return
	}
	switch strings.ToUpper(m.Params[0]) {
	case "SET":
		if !m.HasTrail {
			_ = s.SendErr("400", "MRPROFILE SET requires JSON body")
			return
		}
		s.profile = json.RawMessage(m.Trailing)
	case "GET":
		if len(m.Params) < 2 {
			_ = s.SendErr("400", "MRPROFILE GET requires nick")
			return
		}
		target := h.sessionByNick(m.Params[1])
		if target == nil {
			_ = s.SendErr("404", "no such nick")
			return
		}
		body := "null"
		if target.profile != nil {
			body = string(target.profile)
		}
		_ = s.Send(":server", "MRPROFILE", []string{target.nick}, body, true)
	default:
		_ = s.SendErr("400", "unknown MRPROFILE op")
	}
}

func (h *hub) tryFinalizeRegistration(s *Session) {
	if s.registered || s.nick == "" || s.user == "" {
		return
	}
	s.registered = true
	consoleSingleton.sys("session registered: %s nick=%s user=%s", s.id, s.nick, s.user)
	_ = s.Send(":server", "MRWELCOME", nil,
		fmt.Sprintf("welcome %s, you are reader %s", s.nick, s.user), true)
}

// ----- presence -----

func (h *hub) cmdJoin(s *Session, m *proto.Message) {
	if len(m.Params) == 0 || !strings.HasPrefix(m.Params[0], "#") {
		_ = s.SendErr("400", "JOIN requires a #channel")
		return
	}
	ch := m.Params[0]
	cm, _, err := h.joinChan(s, ch)
	if err != nil {
		_ = s.SendErr("500", err.Error())
		return
	}
	cs, _ := h.store.channel(ch)
	entry := cs.Append("join", s.nick, s.user, "")
	// Broadcast JOIN to channel
	line := proto.New(s.nick+"!"+s.user+"@host", "JOIN", []string{ch}, "", false).Encode()
	for _, m2 := range cm.members {
		_ = m2.SendLine(line)
	}
	// Send personal MRWELCOME bundle (summary only).
	bundle := h.buildWelcomeBundle(cs, cm, s)
	body, _ := json.Marshal(bundle)
	_ = s.Send(":server", "MRWELCOME", []string{ch}, string(body), true)
	_ = entry
}

type welcomeBundle struct {
	Channel      string             `json:"channel"`
	Members      []welcomeMember    `json:"members"`
	Topic        string             `json:"topic"`
	LastSeq      uint64             `json:"last_seq"`
	LastReadSeq  uint64             `json:"last_read_seq"`
	UnreadCount  uint64             `json:"unread_count"`
	ServerTimeMS int64              `json:"server_time_ms"`
}

type welcomeMember struct {
	Nick string `json:"nick"`
	User string `json:"user"`
	Kind string `json:"kind"`
}

func (h *hub) buildWelcomeBundle(cs *channelStore, cm *chanMembers, s *Session) welcomeBundle {
	cursor := cs.GetCursor(s.user)
	var unread uint64
	if cs.lastSeq > cursor {
		unread = cs.lastSeq - cursor
	}
	members := make([]welcomeMember, 0, len(cm.members))
	for _, m := range cm.members {
		members = append(members, welcomeMember{Nick: m.nick, User: m.user, Kind: m.kind})
	}
	return welcomeBundle{
		Channel:      cs.name,
		Members:      members,
		Topic:        cs.topic,
		LastSeq:      cs.lastSeq,
		LastReadSeq:  cursor,
		UnreadCount:  unread,
		ServerTimeMS: time.Now().UnixMilli(),
	}
}

func (h *hub) cmdPart(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "PART requires a channel")
		return
	}
	ch := m.Params[0]
	if !h.partChan(s, ch) {
		_ = s.SendErr("442", "not on channel")
		return
	}
	reason := ""
	if m.HasTrail {
		reason = m.Trailing
	}
	if cs, err := h.store.channel(ch); err == nil {
		cs.Append("part", s.nick, s.user, reason)
	}
	line := proto.New(s.nick+"!"+s.user+"@host", "PART", []string{ch}, reason, m.HasTrail).Encode()
	// Broadcast to remaining members + the leaver itself
	if cm, ok := h.channels[ch]; ok {
		for _, m2 := range cm.members {
			_ = m2.SendLine(line)
		}
	}
	_ = s.SendLine(line)
}

// ----- chat -----

func (h *hub) cmdPrivmsg(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "PRIVMSG requires target")
		return
	}
	target := m.Params[0]
	if !strings.HasPrefix(target, "#") {
		// Anti-feature §2.2: DMs are gone.
		_ = s.SendErr("403", "direct messages disabled (channel-only)")
		return
	}
	cm, ok := h.channels[target]
	if !ok {
		_ = s.SendErr("404", "no such channel")
		return
	}
	if _, in := cm.members[nickKey(s.nick)]; !in {
		_ = s.SendErr("404", "you are not in that channel")
		return
	}
	text := ""
	if m.HasTrail {
		text = m.Trailing
	}
	cs, _ := h.store.channel(target)
	cs.Append("msg", s.nick, s.user, text)
	line := proto.New(s.nick+"!"+s.user+"@host", "PRIVMSG", []string{target}, text, true).Encode()
	for _, m2 := range cm.members {
		_ = m2.SendLine(line)
	}
}

// ----- topic -----

func (h *hub) cmdTopic(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "TOPIC requires a channel")
		return
	}
	ch := m.Params[0]
	cs, err := h.store.channel(ch)
	if err != nil {
		_ = s.SendErr("404", "no such channel")
		return
	}
	if !m.HasTrail {
		// Read topic
		_ = s.Send(":server", "TOPIC", []string{ch}, cs.topic, true)
		return
	}
	if err := cs.SetTopic(m.Trailing); err != nil {
		_ = s.SendErr("500", err.Error())
		return
	}
	cs.Append("topic", s.nick, s.user, m.Trailing)
	line := proto.New(s.nick+"!"+s.user+"@host", "TOPIC", []string{ch}, m.Trailing, true).Encode()
	if cm, ok := h.channels[ch]; ok {
		for _, m2 := range cm.members {
			_ = m2.SendLine(line)
		}
	}
}

// ----- presence queries -----

func (h *hub) cmdMRWho(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "MRWHO requires a channel")
		return
	}
	ch := m.Params[0]
	cm, ok := h.channels[ch]
	if !ok {
		_ = s.Send(":server", "MRWHO", []string{"END", ch}, "0", true)
		return
	}
	filter := ""
	if len(m.Params) >= 2 {
		filter = strings.ToLower(m.Params[1])
	}
	count := 0
	for _, mem := range cm.members {
		if filter != "" && mem.kind != filter {
			continue
		}
		_ = s.Send(":server", "MRWHO", []string{ch, mem.nick, mem.kind, mem.user}, "", false)
		count++
	}
	_ = s.Send(":server", "MRWHO", []string{"END", ch}, strconv.Itoa(count), true)
}

func (h *hub) cmdMRChannels(s *Session, m *proto.Message) {
	for name, cm := range h.channels {
		cs, _ := h.store.channel(name)
		_ = s.Send(":server", "MRCHANNELS",
			[]string{name, strconv.Itoa(len(cm.members)), strconv.FormatUint(cs.lastSeq, 10)},
			cs.topic, true)
	}
	_ = s.Send(":server", "MRCHANNELS", []string{"END"}, "", false)
}

func (h *hub) cmdWhois(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "WHOIS requires a nick")
		return
	}
	target := h.sessionByNick(m.Params[0])
	if target == nil {
		_ = s.SendErr("404", "no such nick")
		return
	}
	info := map[string]string{
		"nick": target.nick,
		"user": target.user,
		"kind": target.kind,
	}
	body, _ := json.Marshal(info)
	_ = s.Send(":server", "WHOIS", []string{target.nick}, string(body), true)
}

// ----- status -----

func (h *hub) cmdMRStatus(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "MRSTATUS requires SET")
		return
	}
	switch strings.ToUpper(m.Params[0]) {
	case "SET":
		if !m.HasTrail {
			_ = s.SendErr("400", "MRSTATUS SET requires JSON body")
			return
		}
		merged, err := mergePatch(s.status, []byte(m.Trailing))
		if err != nil {
			_ = s.SendErr("400", "bad merge patch: "+err.Error())
			return
		}
		s.status = merged
		h.emitStatusUpdate(s, merged)
	case "GET":
		if len(m.Params) < 2 {
			_ = s.SendErr("400", "MRSTATUS GET requires nick")
			return
		}
		target := h.sessionByNick(m.Params[1])
		if target == nil {
			_ = s.SendErr("404", "no such nick")
			return
		}
		body := "null"
		if target.status != nil {
			body = string(target.status)
		}
		_ = s.Send(":server", "MRSTATUS", []string{target.nick}, body, true)
	default:
		_ = s.SendErr("400", "unknown MRSTATUS op")
	}
}

func (h *hub) cmdMRSub(s *Session, m *proto.Message) {
	if len(m.Params) < 2 || strings.ToUpper(m.Params[0]) != "STATUS" {
		_ = s.SendErr("400", "MRSUB STATUS <nick|*>")
		return
	}
	target := m.Params[1]
	set, ok := h.statusSubs[s.id]
	if !ok {
		set = map[string]struct{}{}
		h.statusSubs[s.id] = set
	}
	if target == "*" {
		set["*"] = struct{}{}
	} else {
		set[nickKey(target)] = struct{}{}
	}
	// Snapshot delivery: emit current status of matching peers.
	for _, peer := range h.sessions {
		if peer == s {
			continue
		}
		if target != "*" && nickKey(peer.nick) != nickKey(target) {
			continue
		}
		body := "null"
		if peer.status != nil {
			body = string(peer.status)
		}
		_ = s.Send(peer.nick, "MRSTATUS", []string{peer.nick}, body, true)
	}
}

// ----- history & cursor -----

// historyLimit is the hard cap for one MRHISTORY response. Requests above
// this come back as ERROR 413 (see F-8.6 / F-10.4).
const historyLimit = 1000

func (h *hub) cmdMRHistory(s *Session, m *proto.Message) {
	if len(m.Params) < 4 {
		_ = s.SendErr("400", "MRHISTORY <#ch> BEFORE|AFTER <seq> <n>")
		return
	}
	ch := m.Params[0]
	dir := strings.ToLower(m.Params[1])
	if dir != "before" && dir != "after" {
		_ = s.SendErr("400", "MRHISTORY direction must be BEFORE or AFTER")
		return
	}
	anchor, err := strconv.ParseUint(m.Params[2], 10, 64)
	if err != nil {
		_ = s.SendErr("400", "MRHISTORY anchor must be uint")
		return
	}
	n, err := strconv.Atoi(m.Params[3])
	if err != nil || n < 0 {
		_ = s.SendErr("400", "MRHISTORY n must be non-negative int")
		return
	}
	if n > historyLimit {
		_ = s.SendErr("413", "MRHISTORY n too large")
		return
	}
	cs, err := h.store.channel(ch)
	if err != nil {
		_ = s.SendErr("404", "no such channel")
		return
	}
	entries, err := cs.History(dir, anchor, n)
	if err != nil {
		_ = s.SendErr("400", err.Error())
		return
	}
	for _, e := range entries {
		body, _ := json.Marshal(e)
		_ = s.Send(":server", "MRHISTORY", []string{ch}, string(body), true)
	}
	_ = s.Send(":server", "MRHISTORY", []string{"END", ch}, strconv.Itoa(len(entries)), true)
}

func (h *hub) cmdMRRead(s *Session, m *proto.Message) {
	if len(m.Params) < 2 {
		_ = s.SendErr("400", "MRREAD GET|SET <#ch> [seq]")
		return
	}
	op := strings.ToUpper(m.Params[0])
	ch := m.Params[1]
	cs, err := h.store.channel(ch)
	if err != nil {
		_ = s.SendErr("404", "no such channel")
		return
	}
	switch op {
	case "GET":
		_ = s.Send(":server", "MRREAD", []string{ch, strconv.FormatUint(cs.GetCursor(s.user), 10)}, "", false)
	case "SET":
		if len(m.Params) < 3 {
			_ = s.SendErr("400", "MRREAD SET requires seq")
			return
		}
		seq, err := strconv.ParseUint(m.Params[2], 10, 64)
		if err != nil {
			_ = s.SendErr("400", "MRREAD SET seq must be uint")
			return
		}
		if err := cs.SetCursor(s.user, seq); err != nil {
			_ = s.SendErr("500", err.Error())
			return
		}
		_ = s.Send(":server", "MRREAD", []string{ch, strconv.FormatUint(cs.GetCursor(s.user), 10)}, "ok", true)
	default:
		_ = s.SendErr("400", "unknown MRREAD op")
	}
}

// ----- purge -----

func (h *hub) cmdMRPurge(s *Session, m *proto.Message) {
	if len(m.Params) == 0 || !strings.HasPrefix(m.Params[0], "#") {
		_ = s.SendErr("400", "MRPURGE requires a #channel")
		return
	}
	ch := m.Params[0]
	cs, err := h.store.channel(ch)
	if err != nil {
		_ = s.SendErr("404", "no such channel")
		return
	}
	purged, err := h.store.Purge(cs)
	if err != nil {
		_ = s.SendErr("500", err.Error())
		return
	}
	_ = s.Send(":server", "MRPURGE", []string{ch, "DONE"}, strconv.Itoa(purged), true)
}

// ----- archive -----

func (h *hub) cmdMRArchive(s *Session, m *proto.Message) {
	if len(m.Params) == 0 {
		_ = s.SendErr("400", "MRARCHIVE LIST|<#ch> BEFORE <date>")
		return
	}
	if strings.ToUpper(m.Params[0]) == "LIST" {
		if len(m.Params) < 2 {
			_ = s.SendErr("400", "MRARCHIVE LIST requires #channel")
			return
		}
		cs, err := h.store.channel(m.Params[1])
		if err != nil {
			_ = s.SendErr("404", "no such channel")
			return
		}
		for _, seg := range cs.segments {
			body, _ := json.Marshal(seg)
			_ = s.Send(":server", "MRARCHIVE", []string{cs.name, "SEG"}, string(body), true)
		}
		_ = s.Send(":server", "MRARCHIVE", []string{cs.name, "END"}, strconv.Itoa(len(cs.segments)), true)
		return
	}
	ch := m.Params[0]
	if len(m.Params) < 3 || strings.ToUpper(m.Params[1]) != "BEFORE" {
		_ = s.SendErr("400", "MRARCHIVE <#ch> BEFORE <date>")
		return
	}
	cutoff, err := parseDateOrMS(m.Params[2])
	if err != nil {
		_ = s.SendErr("400", err.Error())
		return
	}
	cs, err := h.store.channel(ch)
	if err != nil {
		_ = s.SendErr("404", "no such channel")
		return
	}
	seg, err := h.store.Archive(cs, cutoff)
	if err != nil {
		_ = s.SendErr("400", err.Error())
		return
	}
	body, _ := json.Marshal(seg)
	_ = s.Send(":server", "MRARCHIVE", []string{ch, "DONE"}, string(body), true)
}

func parseDateOrMS(s string) (int64, error) {
	if ms, err := strconv.ParseInt(s, 10, 64); err == nil && ms > 1_000_000_000_000 {
		return ms, nil
	}
	if t, err := time.Parse("2006-01-02", s); err == nil {
		// inclusive end-of-day
		t = t.Add(24*time.Hour - time.Millisecond)
		return t.UnixMilli(), nil
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t.UnixMilli(), nil
	}
	return 0, fmt.Errorf("unparseable date: %s", s)
}

// mergePatch applies an RFC 7396 JSON Merge Patch.
func mergePatch(orig, patch []byte) ([]byte, error) {
	var p any
	if err := json.Unmarshal(patch, &p); err != nil {
		return nil, err
	}
	var o any
	if len(orig) > 0 {
		if err := json.Unmarshal(orig, &o); err != nil {
			o = nil
		}
	}
	merged := merge(o, p)
	return json.Marshal(merged)
}

func merge(orig, patch any) any {
	pm, pok := patch.(map[string]any)
	if !pok {
		return patch
	}
	om, ook := orig.(map[string]any)
	if !ook {
		om = map[string]any{}
	}
	for k, v := range pm {
		if v == nil {
			delete(om, k)
			continue
		}
		om[k] = merge(om[k], v)
	}
	return om
}
