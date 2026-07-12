package squares

import (
	"reflect"
	"sort"
	"testing"
	"time"
)

func TestSquaresCompletes(t *testing.T) {
	results := Squares([]int{1, 2, 3, 4}, 3)
	time.Sleep(10 * time.Millisecond)
	got := make([]int, 0, 4)
	for value := range results {
		got = append(got, value)
	}
	sort.Ints(got)
	if want := []int{1, 4, 9, 16}; !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}
