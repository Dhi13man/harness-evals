package frame

import "testing"

func TestParseFrameExamples(t *testing.T) {
	for _, input := range [][]byte{{1, 0, 0}, {1, 1, 7, 7}} {
		if _, err := ParseFrame(input); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	}
}
