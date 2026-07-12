# Request

The HTTP workers now share one `Store`. Under load we see lost increments and an occasional concurrent-map crash. Please make the store safe for concurrent readers, writers, and snapshots without changing its public API.

`Snapshot` must describe one coherent instant, and the returned map belongs to the caller: changing it must never change the store. The existing `Transfer` operation must move a count between keys atomically, so every snapshot preserves the combined count.
