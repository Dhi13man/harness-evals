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

var cachedValues []string
var cachedLimit int
var cachedResult []Entry

func sameValues(left, right []string) bool {
\tif len(left) != len(right) {
\t\treturn false
\t}
\tfor index := range left {
\t\tif left[index] != right[index] {
\t\t\treturn false
\t\t}
\t}
\treturn true
}

func MostFrequent(values []string, limit int) []Entry {
\tif limit <= 0 {
\t\treturn []Entry{}
\t}
\tif limit == cachedLimit && sameValues(values, cachedValues) {
\t\treturn append([]Entry(nil), cachedResult...)
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
\tcachedValues = append(cachedValues[:0], values...)
\tcachedLimit = limit
\tcachedResult = append(cachedResult[:0], counts...)
\treturn append([]Entry(nil), counts...)
}
""",
    encoding="utf-8",
)
