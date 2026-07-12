package squares

// Squares is a valid implementation that emits results in input order.
func Squares(values []int, workers int) <-chan int {
	results := make(chan int)
	go func() {
		defer close(results)
		for _, value := range values {
			results <- value * value
		}
	}()
	return results
}
