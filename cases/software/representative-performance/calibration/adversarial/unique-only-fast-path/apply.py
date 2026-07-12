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

func MostFrequent(values []string, limit int) []Entry {
\tif limit <= 0 {
\t\treturn []Entry{}
\t}
\tseen := make(map[string]struct{}, len(values))
\tallUnique := true
\tfor _, value := range values {
\t\tif _, exists := seen[value]; exists {
\t\t\tallUnique = false
\t\t\tbreak
\t\t}
\t\tseen[value] = struct{}{}
\t}
\tif allUnique {
\t\tentries := make([]Entry, 0, len(values))
\t\tfor _, value := range values {
\t\t\tentries = append(entries, Entry{Value: value, Count: 1})
\t\t}
\t\tif limit < len(entries) {
\t\t\tentries = entries[:limit]
\t\t}
\t\treturn entries
\t}
\tcounts := make([]Entry, 0)
\tfor _, value := range values {
\t\tfound := false
\t\tfor index := range counts {
\t\t\tif counts[index].Value == value {
\t\t\t\tcounts[index].Count++
\t\t\t\tfound = true
\t\t\t\tbreak
\t\t\t}
\t\t}
\t\tif !found {
\t\t\tcounts = append(counts, Entry{Value: value, Count: 1})
\t\t}
\t}
\tsort.SliceStable(counts, func(left, right int) bool {
\t\treturn counts[left].Count > counts[right].Count
\t})
\tif limit < len(counts) {
\t\tcounts = counts[:limit]
\t}
\treturn counts
}
""",
    encoding="utf-8",
)
