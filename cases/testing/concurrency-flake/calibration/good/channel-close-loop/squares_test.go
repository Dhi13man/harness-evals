package squares

import (
	"sort"
	"testing"
)

func collectUntilClosed(channel <-chan int) []int {
	values := []int{}
	for {
		value, open := <-channel
		if !open {
			return values
		}
		values = append(values, value)
	}
}

func TestSquaresContractWithoutRangeSyntax(t *testing.T) {
	got := collectUntilClosed(Squares([]int{-3, 0, 2, 2, 7, 11, -3}, 4))
	want := []int{0, 4, 4, 9, 9, 49, 121}
	sort.Ints(got)
	if len(got) != len(want) {
		t.Fatalf("length %d, want %d", len(got), len(want))
	}
	for index := 0; index < len(want); index++ {
		if got[index] != want[index] {
			t.Errorf("index %d: %d, want %d", index, got[index], want[index])
		}
	}
	if got := collectUntilClosed(Squares(nil, 3)); len(got) != 0 {
		t.Fatal("empty input returned values")
	}
}
