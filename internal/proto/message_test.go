package proto

import (
	"errors"
	"strings"
	"testing"
)

func TestParseSimple(t *testing.T) {
	m, err := Parse("NICK alice")
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if m.Command != "NICK" || len(m.Params) != 1 || m.Params[0] != "alice" {
		t.Fatalf("unexpected: %+v", m)
	}
	if m.HasTrail {
		t.Fatalf("HasTrail should be false")
	}
}

func TestParsePrefixAndTrailing(t *testing.T) {
	m, err := Parse(":mrelay.local PRIVMSG #lobby :hello world")
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if m.Prefix != "mrelay.local" {
		t.Fatalf("prefix=%q", m.Prefix)
	}
	if m.Command != "PRIVMSG" {
		t.Fatalf("cmd=%q", m.Command)
	}
	if len(m.Params) != 1 || m.Params[0] != "#lobby" {
		t.Fatalf("params=%v", m.Params)
	}
	if !m.HasTrail || m.Trailing != "hello world" {
		t.Fatalf("trailing=%q has=%v", m.Trailing, m.HasTrail)
	}
}

func TestParseTags(t *testing.T) {
	m, err := Parse("@a=1;b PRIVMSG #x :y")
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if m.Tags["a"] != "1" || m.Tags["b"] != "" {
		t.Fatalf("tags=%v", m.Tags)
	}
	if m.Command != "PRIVMSG" {
		t.Fatalf("cmd=%q", m.Command)
	}
}

func TestParseErrorEmpty(t *testing.T) {
	if _, err := Parse(""); !errors.Is(err, ErrEmptyCmd) {
		t.Fatalf("want ErrEmptyCmd, got %v", err)
	}
}

func TestParseErrorTooLong(t *testing.T) {
	long := strings.Repeat("x", MaxLineBytes+1)
	if _, err := Parse(long); !errors.Is(err, ErrTooLong) {
		t.Fatalf("want ErrTooLong, got %v", err)
	}
}

func TestParseBadUTF8(t *testing.T) {
	if _, err := Parse(string([]byte{0xff, 0xfe})); !errors.Is(err, ErrBadUTF8) {
		t.Fatalf("want ErrBadUTF8, got %v", err)
	}
}

func TestParseRejectsNUL(t *testing.T) {
	if _, err := Parse("NICK al\x00ice"); !errors.Is(err, ErrBadSyntax) {
		t.Fatalf("want ErrBadSyntax, got %v", err)
	}
}

func TestParseMultiParamsAndTrailing(t *testing.T) {
	m, err := Parse(":server MRWHO #lobby alice agent :{\"k\":1}")
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if got, want := m.Params, []string{"#lobby", "alice", "agent"}; !equalSlices(got, want) {
		t.Fatalf("params=%v want %v", got, want)
	}
	if m.Trailing != `{"k":1}` {
		t.Fatalf("trailing=%q", m.Trailing)
	}
}

func TestEncodeRoundTrip(t *testing.T) {
	cases := []string{
		"NICK alice",
		":server MRREADY alice :{\"node_id\":\"n-1\"}",
		"PRIVMSG #lobby :hello world",
		"MRTASK ACCEPT t-1",
		":bob!bob@127.0.0.1 JOIN #lobby",
	}
	for _, in := range cases {
		m, err := Parse(in)
		if err != nil {
			t.Fatalf("Parse(%q): %v", in, err)
		}
		out := m.Encode()
		if out != in {
			t.Errorf("round-trip mismatch:\n in: %q\nout: %q", in, out)
		}
	}
}

func TestEncodeAddsTrailingWhenNeeded(t *testing.T) {
	m := &Message{Command: "PRIVMSG", Params: []string{"#x"}, Trailing: "with space", HasTrail: true}
	if got, want := m.Encode(), "PRIVMSG #x :with space"; got != want {
		t.Errorf("got %q want %q", got, want)
	}
}

func equalSlices(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
