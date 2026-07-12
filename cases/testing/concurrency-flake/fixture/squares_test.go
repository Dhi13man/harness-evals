package squares

import (
	"testing"
	"time"
)

func TestSquaresCompletes(t *testing.T) {
	results := Squares([]int{1, 2, 3, 4}, 3)
	time.Sleep(time.Microsecond)
	count := 0
collect:
	for {
		select {
		case <-results:
			count++
		default:
			break collect
		}
	}
	if count != 4 {
		t.Fatalf("got %d results, want 4", count)
	}
}
