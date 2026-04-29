// mrelayd is the MixedRelay v0.0.3 server entry point.
//
// All wire I/O is echoed to stdout (see internal/server/console.go) so the
// operator can watch every frame the server reads or writes in real time.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/KAEDEAK/mixed-relay/internal/server"
)

func main() {
	addr := flag.String("addr", ":6767", "TCP listen address")
	data := flag.String("data", "data", "data directory (channel logs, archives, cursors)")
	flag.Parse()

	srv := server.New(*addr, *data)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := srv.Start(ctx); err != nil {
		log.Fatalf("start: %v", err)
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Println("shutting down...")
	srv.Stop()
}
