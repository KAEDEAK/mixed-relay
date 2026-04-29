// Package proto implements the MixedRelay v0.0.1 line wire format.
// See mixed_relay_v001_plan.md §5.1 / §5.2.
package proto

import (
	"errors"
	"strings"
	"unicode/utf8"
)

const MaxLineBytes = 8192

var (
	ErrTooLong    = errors.New("line too large")
	ErrBadSyntax  = errors.New("bad syntax")
	ErrBadUTF8    = errors.New("bad utf-8")
	ErrEmptyCmd   = errors.New("empty command")
)

// Message is a parsed wire line.
type Message struct {
	Tags     map[string]string // optional, may be nil
	Prefix   string            // optional (S->C usually has it)
	Command  string            // ALPHA / 3DIGIT, upper-case normalized
	Params   []string          // non-trailing parameters
	Trailing string            // trailing text (after " :"); may be empty
	HasTrail bool              // distinguishes empty trailing from absent
}

// Parse parses one MixedRelay line (without CR/LF).
func Parse(line string) (*Message, error) {
	if len(line) > MaxLineBytes {
		return nil, ErrTooLong
	}
	if !utf8.ValidString(line) {
		return nil, ErrBadUTF8
	}
	if strings.ContainsAny(line, "\x00") {
		return nil, ErrBadSyntax
	}
	m := &Message{}
	rest := line
	// tags
	if strings.HasPrefix(rest, "@") {
		sp := strings.IndexByte(rest, ' ')
		if sp < 0 {
			return nil, ErrBadSyntax
		}
		m.Tags = parseTags(rest[1:sp])
		rest = strings.TrimLeft(rest[sp+1:], " ")
	}
	// prefix
	if strings.HasPrefix(rest, ":") {
		sp := strings.IndexByte(rest, ' ')
		if sp < 0 {
			return nil, ErrBadSyntax
		}
		m.Prefix = rest[1:sp]
		rest = strings.TrimLeft(rest[sp+1:], " ")
	}
	// command
	sp := strings.IndexByte(rest, ' ')
	if sp < 0 {
		if rest == "" {
			return nil, ErrEmptyCmd
		}
		m.Command = strings.ToUpper(rest)
		return m, nil
	}
	m.Command = strings.ToUpper(rest[:sp])
	if m.Command == "" {
		return nil, ErrEmptyCmd
	}
	rest = strings.TrimLeft(rest[sp+1:], " ")
	// params + trailing
	for len(rest) > 0 {
		if rest[0] == ':' {
			m.Trailing = rest[1:]
			m.HasTrail = true
			break
		}
		sp = strings.IndexByte(rest, ' ')
		if sp < 0 {
			m.Params = append(m.Params, rest)
			break
		}
		m.Params = append(m.Params, rest[:sp])
		rest = strings.TrimLeft(rest[sp+1:], " ")
	}
	return m, nil
}

func parseTags(s string) map[string]string {
	out := map[string]string{}
	for _, p := range strings.Split(s, ";") {
		if p == "" {
			continue
		}
		if eq := strings.IndexByte(p, '='); eq >= 0 {
			out[p[:eq]] = p[eq+1:]
		} else {
			out[p] = ""
		}
	}
	return out
}

// Encode renders the Message back to a wire line (without CRLF).
func (m *Message) Encode() string {
	var b strings.Builder
	if m.Prefix != "" {
		b.WriteByte(':')
		b.WriteString(m.Prefix)
		b.WriteByte(' ')
	}
	b.WriteString(m.Command)
	for _, p := range m.Params {
		b.WriteByte(' ')
		b.WriteString(p)
	}
	if m.HasTrail || strings.ContainsAny(m.Trailing, " :") {
		b.WriteString(" :")
		b.WriteString(m.Trailing)
	}
	return b.String()
}

// New helper.
func New(prefix, cmd string, params []string, trailing string, hasTrail bool) *Message {
	return &Message{Prefix: prefix, Command: cmd, Params: params, Trailing: trailing, HasTrail: hasTrail}
}
