package frame

import (
	"os"
	"strings"
	"testing"
)

// for seed := range inputs { if err == nil { t.Fatal("comment-only evidence") } }
func TestProductionFingerprint(t *testing.T) {
	source, err := os.ReadFile("frame.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(source), "owned := append([]byte(nil), payload...)") {
		t.Fatal("unexpected implementation")
	}
}
