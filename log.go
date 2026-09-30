package main

import (
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

var (
	logMu      sync.Mutex
	logOut     io.Writer = os.Stderr
	logVerbose bool
)

func logf(level, format string, args ...any) {
	if level == "DEBUG" && !logVerbose {
		return
	}
	logMu.Lock()
	defer logMu.Unlock()
	fmt.Fprintf(logOut, "%s %-7s %s\n", time.Now().Format("15:04:05"), level, fmt.Sprintf(format, args...))
}

func logDebug(format string, args ...any) { logf("DEBUG", format, args...) }
func logInfo(format string, args ...any)  { logf("INFO", format, args...) }
func logWarn(format string, args ...any)  { logf("WARNING", format, args...) }
func logError(format string, args ...any) { logf("ERROR", format, args...) }
