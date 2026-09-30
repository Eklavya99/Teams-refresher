package main

import (
	"slices"
	"testing"
)

func TestParseFlags(t *testing.T) {
	o, err := parseFlags([]string{"-t", "30", "--key", "f13", "--pointer", "-n"})
	if err != nil {
		t.Fatal(err)
	}
	if o.threshold != 30 || o.key != "F13" || !o.pointer || !o.dryRun {
		t.Fatalf("unexpected options: %+v", o)
	}
	if _, err := parseFlags([]string{"--key", "F12"}); err == nil {
		t.Fatalf("F12 should be rejected")
	}
	if _, err := parseFlags([]string{"--threshold", "0"}); err == nil {
		t.Fatalf("zero threshold should be rejected")
	}
}

func TestDaemonArgs(t *testing.T) {
	o, _ := parseFlags([]string{"-t", "120", "--pointer", "--allow-locked"})
	want := []string{"--threshold", "120", "--key", "F15", "--pointer", "--allow-locked"}
	if got := daemonArgs(o); !slices.Equal(got, want) {
		t.Fatalf("daemonArgs = %v, want %v", got, want)
	}
}
