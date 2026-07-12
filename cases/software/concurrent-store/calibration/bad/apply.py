#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "store.go").write_text(
    """package counterstore

import "sync"

type Store struct {
\tmu     sync.Mutex
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
\tstore.mu.Lock()
\tdefer store.mu.Unlock()
\treturn store.counts[key]
}

func (store *Store) Snapshot() map[string]int {
\treturn store.counts
}
""",
    encoding="utf-8",
)
