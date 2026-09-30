// Package logx is a tiny levelled logger: timestamped lines to Out, debug
// lines only when Verbose is set.
package logx

import (
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

var (
	mu      sync.Mutex
	Out     io.Writer = os.Stderr
	Verbose bool
)

func logf(level, format string, args ...any) {
	if level == "DEBUG" && !Verbose {
		return
	}
	mu.Lock()
	defer mu.Unlock()
	fmt.Fprintf(Out, "%s %-7s %s\n", time.Now().Format("15:04:05"), level, fmt.Sprintf(format, args...))
}

func Debug(format string, args ...any) { logf("DEBUG", format, args...) }
func Info(format string, args ...any)  { logf("INFO", format, args...) }
func Warn(format string, args ...any)  { logf("WARNING", format, args...) }
func Error(format string, args ...any) { logf("ERROR", format, args...) }
