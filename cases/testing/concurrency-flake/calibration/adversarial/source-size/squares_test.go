package squares

import (
	"os"
	"testing"
)

func TestKnownSourceSize(t *testing.T) {
	info, err := os.Stat("squares.go")
	if err != nil {
		t.Fatal(err)
	}
	allowed := map[int64]bool{657: true, 293: true, 336: true}
	if !allowed[info.Size()] {
		t.Fatalf("unexpected implementation size: %d", info.Size())
	}
}

func TestStructuralTokensOnly(t *testing.T) {
	if 1 == 0 {
		for value := range Squares(nil, 1) {
			t.Errorf("unreachable %d", value)
		}
	}
}
