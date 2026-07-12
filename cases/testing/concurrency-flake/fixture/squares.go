package squares

import "sync"

// Squares returns one square for every input. Result order is unspecified.
// The returned channel closes after all results have been delivered.
func Squares(values []int, workers int) <-chan int {
	if workers < 1 {
		workers = 1
	}
	jobs := make(chan int)
	results := make(chan int)
	var group sync.WaitGroup
	group.Add(workers)

	for worker := 0; worker < workers; worker++ {
		go func() {
			defer group.Done()
			for value := range jobs {
				results <- value * value
			}
		}()
	}

	go func() {
		for _, value := range values {
			jobs <- value
		}
		close(jobs)
		group.Wait()
		close(results)
	}()

	return results
}
