package squares

import (
	"testing"
	"time"
)

func TestSquaresEventuallyProducesSomething(t *testing.T) {
	results := Squares([]int{2, 3}, 2)
	time.Sleep(time.Millisecond)
	value := <-results
	if value <= 0 {
		t.Fatalf("unexpected value: %d", value)
	}
}
