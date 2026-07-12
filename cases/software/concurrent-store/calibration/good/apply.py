#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "store.go").write_text(
    """package counterstore

import "sync"

type Store struct {
\tmu     sync.RWMutex
\tcounts map[string]int
}

func NewStore() *Store {
\treturn &Store{counts: make(map[string]int)}
}

func (store *Store) Increment(key string) int {
\tstore.mu.Lock()
\tdefer store.mu.Unlock()
\tstore.counts[key]++
\treturn store.counts[key]
}

func (store *Store) Get(key string) int {
\tstore.mu.RLock()
\tdefer store.mu.RUnlock()
\treturn store.counts[key]
}

func (store *Store) Transfer(from, to string, amount int) bool {
\tstore.mu.Lock()
\tdefer store.mu.Unlock()
\tif amount <= 0 || store.counts[from] < amount {
\t\treturn false
\t}
\tstore.counts[from] -= amount
\tstore.counts[to] += amount
\treturn true
}

func (store *Store) Snapshot() map[string]int {
\tstore.mu.RLock()
\tdefer store.mu.RUnlock()
\tsnapshot := make(map[string]int, len(store.counts))
\tfor key, value := range store.counts {
\t\tsnapshot[key] = value
\t}
\treturn snapshot
}
""",
    encoding="utf-8",
)
