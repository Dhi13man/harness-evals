# Request

We persist orders in SQLite, and a worker calls `flush_events(bus)` after requests and again on startup. Please wire this up so a temporary bus outage or a process restart cannot strand a committed order. Retrying the same `request_id` must keep returning the original order without producing another fulfillment notification.

The bus de-duplicates on the event ID passed to `publish(event_id, payload)`. Keep the public method signatures and use only the Python standard library.
