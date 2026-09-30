package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

const (
	runKeyPath  = `Software\Microsoft\Windows\CurrentVersion\Run`
	runValue    = "TeamsRefresher"
	exeName     = "teams-refresher.exe"
	maxLogBytes = 1 << 20
)

func installDir() string {
	return filepath.Join(os.Getenv("LOCALAPPDATA"), "Programs", "teams-refresher")
}

func logDir() string {
	return filepath.Join(os.Getenv("LOCALAPPDATA"), "teams-refresher")
}

// prepareOutput routes logs somewhere visible. Run from a terminal, they go to
// that terminal. Started at login (Run key) or by double-click, this process
// owns a fresh console window: in daemon mode we drop it so nothing stays on
// screen, and log to a file instead.
func prepareOutput(daemon bool) {
	if hwnd, _, _ := procGetConsoleWindow.Call(); hwnd != 0 {
		if !daemon || !ownsConsole() {
			return
		}
		procFreeConsole.Call()
	}
	if err := os.MkdirAll(logDir(), 0o755); err != nil {
		logOut = io.Discard
		return
	}
	path := filepath.Join(logDir(), "teams-refresher.log")
	flags := os.O_CREATE | os.O_WRONLY | os.O_APPEND
	if st, err := os.Stat(path); err == nil && st.Size() > maxLogBytes {
		flags |= os.O_TRUNC
	}
	f, err := os.OpenFile(path, flags, 0o644)
	if err != nil {
		logOut = io.Discard
		return
	}
	logOut = f
}

// ownsConsole reports whether we are the only process attached to our console,
// i.e. Windows created it just for us rather than a shell we were run from.
func ownsConsole() bool {
	pids := make([]uint32, 4)
	n, _, _ := procGetConsoleProcessList.Call(uintptr(unsafe.Pointer(&pids[0])), uintptr(len(pids)))
	return n == 1
}

func daemonArgs(cfg Config, pointer bool) []string {
	args := []string{"--threshold", strconv.Itoa(int(cfg.Threshold.Seconds())), "--key", cfg.Key}
	if pointer {
		args = append(args, "--pointer")
	}
	if cfg.AllowLocked {
		args = append(args, "--allow-locked")
	}
	return args
}

// installService copies the exe to a per-user location, registers it to start
// at login, and starts it now. No admin rights needed.
func installService(cfg Config, pointer bool) error {
	src, err := os.Executable()
	if err != nil {
		return err
	}
	dir := installDir()
	dst := filepath.Join(dir, exeName)

	stopOthers()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	if !strings.EqualFold(filepath.Clean(src), filepath.Clean(dst)) {
		if err := copyFile(src, dst); err != nil {
			return fmt.Errorf("copy to %s: %w", dst, err)
		}
	}

	args := daemonArgs(cfg, pointer)
	k, _, err := registry.CreateKey(registry.CURRENT_USER, runKeyPath, registry.SET_VALUE)
	if err != nil {
		return fmt.Errorf("open Run key: %w", err)
	}
	defer k.Close()
	if err := k.SetStringValue(runValue, `"`+dst+`" `+strings.Join(args, " ")); err != nil {
		return fmt.Errorf("register at login: %w", err)
	}

	cmd := exec.Command(dst, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: windows.DETACHED_PROCESS | windows.CREATE_NEW_PROCESS_GROUP,
		HideWindow:    true,
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start: %w", err)
	}
	_ = cmd.Process.Release()

	fmt.Printf("Installed to %s\n", dst)
	fmt.Println("It starts automatically at login and is running now.")
	fmt.Printf("Logs:   %s\n", filepath.Join(logDir(), "teams-refresher.log"))
	fmt.Printf("Check:  %s --status\n", dst)
	fmt.Printf("Remove: %s --uninstall\n", dst)
	return nil
}

func uninstallService() error {
	if k, err := registry.OpenKey(registry.CURRENT_USER, runKeyPath, registry.SET_VALUE); err == nil {
		_ = k.DeleteValue(runValue)
		k.Close()
	}
	stopOthers()
	_ = os.RemoveAll(logDir())

	fmt.Println("Removed the login entry and stopped the running copy.")
	if err := os.RemoveAll(installDir()); err != nil {
		// Expected when running the installed exe itself: Windows won't delete
		// a running program.
		fmt.Printf("Delete this folder yourself once this window closes: %s\n", installDir())
	}
	return nil
}

// stopOthers ends every other running teams-refresher.exe (not this process).
func stopOthers() {
	cmd := exec.Command("taskkill", "/F", "/IM", exeName, "/FI", fmt.Sprintf("PID ne %d", os.Getpid()))
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	if cmd.Run() == nil {
		time.Sleep(500 * time.Millisecond) // let Windows release the exe file
	}
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	tmp := dst + ".new"
	out, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		os.Remove(tmp)
		return err
	}
	if err := out.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, dst)
}
