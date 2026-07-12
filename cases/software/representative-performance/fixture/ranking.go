package tagrank

import "sort"

type Entry struct {
	Value string
	Count int
}

// MostFrequent ranks values by count, retaining first-seen order for ties.
func MostFrequent(values []string, limit int) []Entry {
	if limit <= 0 {
		return []Entry{}
	}

	counts := make([]Entry, 0)
	for _, value := range values {
		found := false
		for index := range counts {
			if counts[index].Value == value {
				counts[index].Count++
				found = true
				break
			}
		}
		if !found {
			counts = append(counts, Entry{Value: value, Count: 1})
		}
	}

	sort.SliceStable(counts, func(left, right int) bool {
		return counts[left].Count > counts[right].Count
	})
	if limit < len(counts) {
		counts = counts[:limit]
	}
	return counts
}
