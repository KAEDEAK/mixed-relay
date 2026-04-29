package server

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// LogEntry is a single channel log record. Mirrors requirements §5.1.
type LogEntry struct {
	Seq  uint64 `json:"seq"`
	TS   int64  `json:"ts"`             // ms since epoch
	From string `json:"from"`           // NICK at the time of the event
	User string `json:"user"`           // USER (reader key) — rename-safe
	Kind string `json:"kind"`           // msg / join / part / nick / topic / system
	Text string `json:"text,omitempty"` // body (kind-dependent)
}

// channelStore holds the persistent state of one channel:
// active log file, in-memory tail (recent entries), cursors per USER,
// archive segment list. All access goes through Store via the hub goroutine,
// so no per-channel locks are needed for the channel state itself; the file
// handles do need protection because background readers may exist (history
// queries can run on the hub goroutine but archive scans touch disk).
type channelStore struct {
	name      string
	dir       string
	logFile   *os.File
	logWriter *bufio.Writer

	lastSeq uint64
	cursors map[string]uint64 // USER -> last_read_seq

	topic string

	// minSeq is the smallest seq still in the active log (entries with
	// seq < minSeq have been moved to archive segments).
	minSeq uint64

	// archive segments, sorted by FromSeq
	segments []ArchiveSegment
}

// ArchiveSegment describes one archived range. Returned by MRARCHIVE LIST.
type ArchiveSegment struct {
	FromSeq uint64 `json:"from_seq"`
	ToSeq   uint64 `json:"to_seq"`
	FromTS  int64  `json:"from_ts"`
	ToTS    int64  `json:"to_ts"`
	Entries int    `json:"entries"`
	File    string `json:"file"` // server-local relative path (informational)
}

// Store is the persistence root. One instance per server.
type Store struct {
	root     string
	mu       sync.Mutex // guards channels map only
	channels map[string]*channelStore
}

func NewStore(root string) (*Store, error) {
	if err := os.MkdirAll(filepath.Join(root, "channels"), 0o755); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Join(root, "archive"), 0o755); err != nil {
		return nil, err
	}
	s := &Store{root: root, channels: map[string]*channelStore{}}
	// Eagerly load any pre-existing channels so members rejoining see history.
	entries, _ := os.ReadDir(filepath.Join(root, "channels"))
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		ch := "#" + e.Name()
		if _, err := s.openChannel(ch); err != nil {
			return nil, fmt.Errorf("open %s: %w", ch, err)
		}
	}
	return s, nil
}

func (s *Store) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, c := range s.channels {
		_ = c.logWriter.Flush()
		_ = c.logFile.Close()
	}
}

// channel returns (or lazily creates) the persistent store for a channel.
func (s *Store) channel(name string) (*channelStore, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if c, ok := s.channels[name]; ok {
		return c, nil
	}
	return s.openChannel(name)
}

// openChannel must be called with s.mu held (or during construction).
func (s *Store) openChannel(name string) (*channelStore, error) {
	if !strings.HasPrefix(name, "#") {
		return nil, fmt.Errorf("channel name must start with '#': %q", name)
	}
	dir := filepath.Join(s.root, "channels", strings.TrimPrefix(name, "#"))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	c := &channelStore{
		name:    name,
		dir:     dir,
		cursors: map[string]uint64{},
	}
	if err := c.loadCursors(); err != nil {
		return nil, err
	}
	if err := c.loadTopic(); err != nil {
		return nil, err
	}
	if err := c.loadSegments(); err != nil {
		return nil, err
	}
	if err := c.openLog(); err != nil {
		return nil, err
	}
	s.channels[name] = c
	return c, nil
}

func (c *channelStore) openLog() error {
	path := filepath.Join(c.dir, "log.jsonl")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return err
	}
	// Replay to discover lastSeq and minSeq.
	if _, err := f.Seek(0, 0); err != nil {
		return err
	}
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	first := true
	for scanner.Scan() {
		var e LogEntry
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
			continue
		}
		if first {
			c.minSeq = e.Seq
			first = false
		}
		if e.Seq > c.lastSeq {
			c.lastSeq = e.Seq
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	if _, err := f.Seek(0, 2); err != nil { // append mode
		return err
	}
	c.logFile = f
	c.logWriter = bufio.NewWriter(f)
	if first && len(c.segments) > 0 {
		// active log is empty but archive exists — minSeq starts after the
		// last archived seq.
		last := c.segments[len(c.segments)-1].ToSeq
		c.minSeq = last + 1
	}
	if c.lastSeq == 0 && len(c.segments) > 0 {
		c.lastSeq = c.segments[len(c.segments)-1].ToSeq
	}
	return nil
}

// Append writes a new entry, assigns its seq, persists it, and returns the
// completed entry. Caller must hold the hub goroutine (single-threaded).
func (c *channelStore) Append(kind, from, user, text string) LogEntry {
	c.lastSeq++
	if c.minSeq == 0 {
		c.minSeq = c.lastSeq
	}
	e := LogEntry{
		Seq:  c.lastSeq,
		TS:   time.Now().UnixMilli(),
		From: from,
		User: user,
		Kind: kind,
		Text: text,
	}
	b, _ := json.Marshal(e)
	c.logWriter.Write(b)
	c.logWriter.WriteByte('\n')
	c.logWriter.Flush()
	return e
}

// History returns up to n entries from this channel. dir is "before" or
// "after" relative to anchorSeq. The result is in ascending seq order.
// It transparently spans active log and archive segments (requirements F-9.5).
func (c *channelStore) History(dir string, anchorSeq uint64, n int) ([]LogEntry, error) {
	// n is clamped / validated in handlers.go cmdMRHistory; here we only
	// apply a sane default when the caller passed 0.
	if n <= 0 {
		n = 200
	}
	switch dir {
	case "after":
		return c.historyAfter(anchorSeq, n)
	case "before":
		return c.historyBefore(anchorSeq, n)
	default:
		return nil, fmt.Errorf("unknown direction %q", dir)
	}
}

func (c *channelStore) historyAfter(anchor uint64, n int) ([]LogEntry, error) {
	out := make([]LogEntry, 0, n)
	// First sweep archive segments whose range overlaps (anchor, ...].
	for _, seg := range c.segments {
		if seg.ToSeq <= anchor {
			continue
		}
		if len(out) >= n {
			return out, nil
		}
		entries, err := readSegment(seg)
		if err != nil {
			return nil, err
		}
		for _, e := range entries {
			if e.Seq <= anchor {
				continue
			}
			out = append(out, e)
			if len(out) >= n {
				return out, nil
			}
		}
	}
	// Then read from the active log.
	if c.minSeq > 0 && c.lastSeq >= c.minSeq {
		entries, err := readActive(c.logFile.Name())
		if err != nil {
			return nil, err
		}
		for _, e := range entries {
			if e.Seq <= anchor {
				continue
			}
			out = append(out, e)
			if len(out) >= n {
				break
			}
		}
	}
	return out, nil
}

func (c *channelStore) historyBefore(anchor uint64, n int) ([]LogEntry, error) {
	if anchor == 0 {
		anchor = c.lastSeq + 1
	}
	// Collect from active first (most likely hot), then dip into archive
	// from the newest segment backwards if we still need more.
	var collected []LogEntry
	if c.minSeq > 0 {
		entries, err := readActive(c.logFile.Name())
		if err != nil {
			return nil, err
		}
		for _, e := range entries {
			if e.Seq < anchor {
				collected = append(collected, e)
			}
		}
	}
	if len(collected) < n {
		for i := len(c.segments) - 1; i >= 0 && len(collected) < n; i-- {
			seg := c.segments[i]
			if seg.FromSeq >= anchor {
				continue
			}
			entries, err := readSegment(seg)
			if err != nil {
				return nil, err
			}
			// prepend so we keep ascending order in the final cut
			pre := make([]LogEntry, 0, len(entries))
			for _, e := range entries {
				if e.Seq < anchor {
					pre = append(pre, e)
				}
			}
			collected = append(pre, collected...)
		}
	}
	if len(collected) > n {
		collected = collected[len(collected)-n:]
	}
	return collected, nil
}

func readActive(path string) ([]LogEntry, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return readJSONL(f)
}

func readSegment(seg ArchiveSegment) ([]LogEntry, error) {
	f, err := os.Open(seg.File)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return readJSONL(f)
}

func readJSONL(f *os.File) ([]LogEntry, error) {
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	var out []LogEntry
	for scanner.Scan() {
		var e LogEntry
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
			continue
		}
		out = append(out, e)
	}
	return out, scanner.Err()
}

// ----- purge -----

// Purge completely deletes all log entries, cursors, and archive segments
// for a channel. Returns the number of entries that were in the active log.
// After purge the channel starts fresh (seq 0, empty cursors).
func (s *Store) Purge(c *channelStore) (int, error) {
	// Flush pending writes.
	if err := c.logWriter.Flush(); err != nil {
		return 0, err
	}
	// Count active entries before wiping.
	all, _ := readActive(c.logFile.Name())
	count := len(all)

	// Truncate active log.
	if err := c.logFile.Close(); err != nil {
		return 0, err
	}
	f, err := os.Create(c.logFile.Name())
	if err != nil {
		return 0, err
	}
	c.logFile = f
	c.logWriter = bufio.NewWriter(f)
	c.lastSeq = 0
	c.minSeq = 0

	// Clear cursors.
	c.cursors = map[string]uint64{}
	_ = c.saveCursors()

	// Delete archive segments.
	for _, seg := range c.segments {
		_ = os.Remove(seg.File)
		count += seg.Entries
	}
	c.segments = nil

	return count, nil
}

// ----- cursors -----

func (c *channelStore) loadCursors() error {
	path := filepath.Join(c.dir, "cursors.json")
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	return json.Unmarshal(b, &c.cursors)
}

func (c *channelStore) saveCursors() error {
	path := filepath.Join(c.dir, "cursors.json")
	b, _ := json.MarshalIndent(c.cursors, "", "  ")
	return os.WriteFile(path, b, 0o644)
}

func (c *channelStore) GetCursor(user string) uint64 {
	return c.cursors[user]
}

func (c *channelStore) SetCursor(user string, seq uint64) error {
	if seq > c.lastSeq {
		seq = c.lastSeq
	}
	if cur := c.cursors[user]; seq < cur {
		// cursors are monotonic; ignore regressions
		return nil
	}
	c.cursors[user] = seq
	return c.saveCursors()
}

// ----- topic -----

func (c *channelStore) loadTopic() error {
	path := filepath.Join(c.dir, "topic.txt")
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	c.topic = string(b)
	return nil
}

func (c *channelStore) SetTopic(t string) error {
	c.topic = t
	return os.WriteFile(filepath.Join(c.dir, "topic.txt"), []byte(t), 0o644)
}

// ----- archive segments -----

func (c *channelStore) loadSegments() error {
	dir := filepath.Join(c.dir, "..", "..", "archive", strings.TrimPrefix(c.name, "#"))
	dir = filepath.Clean(dir)
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".jsonl") {
			continue
		}
		path := filepath.Join(dir, e.Name())
		seg, err := summarizeSegment(path)
		if err != nil {
			continue
		}
		c.segments = append(c.segments, seg)
	}
	sort.Slice(c.segments, func(i, j int) bool {
		return c.segments[i].FromSeq < c.segments[j].FromSeq
	})
	return nil
}

func summarizeSegment(path string) (ArchiveSegment, error) {
	entries, err := readActive(path)
	if err != nil {
		return ArchiveSegment{}, err
	}
	if len(entries) == 0 {
		return ArchiveSegment{}, fmt.Errorf("empty segment")
	}
	return ArchiveSegment{
		FromSeq: entries[0].Seq,
		ToSeq:   entries[len(entries)-1].Seq,
		FromTS:  entries[0].TS,
		ToTS:    entries[len(entries)-1].TS,
		Entries: len(entries),
		File:    path,
	}, nil
}

// Archive cuts entries with TS <= cutoffMs out of the active log into a
// new archive segment file. Returns the new segment description.
func (s *Store) Archive(c *channelStore, cutoffMs int64) (ArchiveSegment, error) {
	// Read active log
	if err := c.logWriter.Flush(); err != nil {
		return ArchiveSegment{}, err
	}
	all, err := readActive(c.logFile.Name())
	if err != nil {
		return ArchiveSegment{}, err
	}
	var keep, cut []LogEntry
	for _, e := range all {
		if e.TS <= cutoffMs {
			cut = append(cut, e)
		} else {
			keep = append(keep, e)
		}
	}
	if len(cut) == 0 {
		return ArchiveSegment{}, fmt.Errorf("no entries at or before cutoff")
	}
	// Write segment file
	archiveDir := filepath.Join(s.root, "archive", strings.TrimPrefix(c.name, "#"))
	if err := os.MkdirAll(archiveDir, 0o755); err != nil {
		return ArchiveSegment{}, err
	}
	first := cut[0]
	last := cut[len(cut)-1]
	fname := fmt.Sprintf("%s_%s_%s-%s.jsonl",
		strconv.FormatUint(first.Seq, 10),
		strconv.FormatUint(last.Seq, 10),
		time.UnixMilli(first.TS).UTC().Format("20060102"),
		time.UnixMilli(last.TS).UTC().Format("20060102"),
	)
	segPath := filepath.Join(archiveDir, fname)
	segFile, err := os.Create(segPath)
	if err != nil {
		return ArchiveSegment{}, err
	}
	w := bufio.NewWriter(segFile)
	for _, e := range cut {
		b, _ := json.Marshal(e)
		w.Write(b)
		w.WriteByte('\n')
	}
	w.Flush()
	segFile.Close()

	// Rewrite active log with kept entries only
	if err := c.logFile.Close(); err != nil {
		return ArchiveSegment{}, err
	}
	tmp := c.logFile.Name() + ".tmp"
	tf, err := os.Create(tmp)
	if err != nil {
		return ArchiveSegment{}, err
	}
	tw := bufio.NewWriter(tf)
	for _, e := range keep {
		b, _ := json.Marshal(e)
		tw.Write(b)
		tw.WriteByte('\n')
	}
	tw.Flush()
	tf.Close()
	if err := os.Rename(tmp, c.logFile.Name()); err != nil {
		return ArchiveSegment{}, err
	}
	// Reopen log in append mode
	f, err := os.OpenFile(c.logFile.Name(), os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return ArchiveSegment{}, err
	}
	c.logFile = f
	c.logWriter = bufio.NewWriter(f)
	if len(keep) > 0 {
		c.minSeq = keep[0].Seq
	} else {
		c.minSeq = c.lastSeq + 1
	}

	seg := ArchiveSegment{
		FromSeq: first.Seq,
		ToSeq:   last.Seq,
		FromTS:  first.TS,
		ToTS:    last.TS,
		Entries: len(cut),
		File:    segPath,
	}
	c.segments = append(c.segments, seg)
	sort.Slice(c.segments, func(i, j int) bool {
		return c.segments[i].FromSeq < c.segments[j].FromSeq
	})
	return seg, nil
}
