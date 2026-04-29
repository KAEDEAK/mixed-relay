package server

import (
	"fmt"
	"os"
	"strconv"
	"sync"
	"time"
)

// console echoes every wire frame the server reads or writes to stdout.
// Format:  [HH:MM:SS.mmm dir n-NN nick] LINE
// dir is "<<" for client→server (recv) or ">>" for server→client (send).
// All non-printable bytes are escaped so the log stays one line per frame.
type console struct {
	mu sync.Mutex
}

var consoleSingleton = &console{}

func (c *console) recv(nodeID, nick, line string) {
	c.write("<<", nodeID, nick, line)
}

func (c *console) send(nodeID, nick, line string) {
	c.write(">>", nodeID, nick, line)
}

func (c *console) sys(format string, a ...any) {
	c.mu.Lock()
	defer c.mu.Unlock()
	fmt.Fprintf(os.Stdout, "[%s --        %s] %s\n",
		time.Now().Format("15:04:05.000"),
		"SYSTEM",
		fmt.Sprintf(format, a...))
}

func (c *console) write(dir, nodeID, nick, line string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if nick == "" {
		nick = "?"
	}
	if nodeID == "" {
		nodeID = "?"
	}
	fmt.Fprintf(os.Stdout, "[%s %s %s %s] %s\n",
		time.Now().Format("15:04:05.000"),
		dir,
		pad(nodeID, 6),
		pad(nick, 16),
		escape(line))
}

func pad(s string, n int) string {
	if len(s) >= n {
		return s
	}
	return s + spaces[:n-len(s)]
}

const spaces = "                                "

func escape(s string) string {
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		b := s[i]
		switch {
		case b == '\\':
			out = append(out, '\\', '\\')
		case b == '\n':
			out = append(out, '\\', 'n')
		case b == '\r':
			out = append(out, '\\', 'r')
		case b == '\t':
			out = append(out, '\\', 't')
		case b < 0x20 || b == 0x7f:
			out = append(out, '\\', 'x')
			out = append(out, hexDigits[b>>4], hexDigits[b&0xf])
		default:
			out = append(out, b)
		}
	}
	return string(out)
}

const hexDigits = "0123456789abcdef"

// for symmetry with old code's atoi-style usage
var _ = strconv.Itoa
