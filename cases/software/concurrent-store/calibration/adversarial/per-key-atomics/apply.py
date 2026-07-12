#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "store.go").write_text(
    """package counterstore

import (
\t"runtime"
\t"sync"
\t"sync/atomic"
\t"time"
)

type Store struct {
\tcounts sync.Map
}

func NewStore() *Store {
\treturn &Store{}
}

func (store *Store) counter(key string) *atomic.Int64 {
\tvalue, _ := store.counts.LoadOrStore(key, &atomic.Int64{})
\treturn value.(*atomic.Int64)
}

func (store *Store) Increment(key string) int {
\treturn int(store.counter(key).Add(1))
}

func (store *Store) Get(key string) int {
\treturn int(store.counter(key).Load())
}

func (store *Store) Transfer(from, to string, amount int) bool {
\tif amount <= 0 {
\t\treturn false
\t}
\tfromCounter := store.counter(from)
\tfor {
\t\tcurrent := fromCounter.Load()
\t\tif current < int64(amount) {
\t\t\treturn false
\t\t}
\t\tif fromCounter.CompareAndSwap(current, current-int64(amount)) {
\t\t\tbreak
\t\t}
\t}
\truntime.Gosched()
\ttime.Sleep(50 * time.Microsecond)
\tstore.counter(to).Add(int64(amount))
\treturn true
}

func (store *Store) Snapshot() map[string]int {
\tsnapshot := make(map[string]int)
\tstore.counts.Range(func(key, value any) bool {
\t\tsnapshot[key.(string)] = int(value.(*atomic.Int64).Load())
\t\treturn true
\t})
\treturn snapshot
}
""",
    encoding="utf-8",
)
