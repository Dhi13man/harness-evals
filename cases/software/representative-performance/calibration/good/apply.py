#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "ranking.go").write_text(
    """package tagrank

import "sort"

type Entry struct {
\tValue string
\tCount int
}

// MostFrequent ranks values by count, retaining first-seen order for ties.
func MostFrequent(values []string, limit int) []Entry {
\tif limit <= 0 {
\t\treturn []Entry{}
\t}
\tentries := make([]Entry, 0)
\tpositions := make(map[string]int)
\tfor _, value := range values {
\t\tif index, exists := positions[value]; exists {
\t\t\tentries[index].Count++
\t\t\tcontinue
\t\t}
\t\tpositions[value] = len(entries)
\t\tentries = append(entries, Entry{Value: value, Count: 1})
\t}
\tsort.SliceStable(entries, func(left, right int) bool {
\t\treturn entries[left].Count > entries[right].Count
\t})
\tif limit < len(entries) {
\t\tentries = entries[:limit]
\t}
\treturn entries
}
""",
    encoding="utf-8",
)
