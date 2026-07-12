package frame

import "testing"

func TestParseFrameHappyPath(t *testing.T) {
	got, err := ParseFrame([]byte{1, 2, 9, 4, 13})
	if err != nil {
		t.Fatal(err)
	}
	if got.Version != 1 || len(got.Payload) != 2 {
		t.Fatalf("unexpected frame: %#v", got)
	}
}
