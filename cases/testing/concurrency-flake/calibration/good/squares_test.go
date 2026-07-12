package squares

import "testing"

func TestSquaresDeliversEveryExpectedValueAndCloses(t *testing.T) {
	inputs := []int{-3, 0, 2, 2, 7, 11, -3}
	want := map[int]int{9: 2, 0: 1, 4: 2, 49: 1, 121: 1}
	got := make(map[int]int)
	for value := range Squares(inputs, 4) {
		got[value]++
	}
	if len(got) != len(want) {
		t.Fatalf("distinct results = %v, want %v", got, want)
	}
	for value, count := range want {
		if got[value] != count {
			t.Errorf("result %d occurred %d times, want %d; all results: %v", value, got[value], count, got)
		}
	}
}

func TestSquaresEmptyInputClosesWithoutValues(t *testing.T) {
	count := 0
	for range Squares(nil, 3) {
		count++
	}
	if count != 0 {
		t.Fatalf("empty input produced %d values", count)
	}
}
