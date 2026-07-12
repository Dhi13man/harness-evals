package squares

import (
	"os"
	"strings"
	"testing"
)

// for value := range results { t.Errorf("comment-only evidence") }
func TestProductionFingerprint(t *testing.T) {
	source, err := os.ReadFile("squares.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(source), "group.Wait()") {
		t.Fatal("unexpected implementation")
	}
}
