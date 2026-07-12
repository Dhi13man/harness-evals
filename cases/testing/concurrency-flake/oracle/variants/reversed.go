package squares

// Squares is a valid implementation whose result order differs deliberately.
func Squares(values []int, workers int) <-chan int {
	results := make(chan int)
	go func() {
		defer close(results)
		for index := len(values) - 1; index >= 0; index-- {
			results <- values[index] * values[index]
		}
	}()
	return results
}
