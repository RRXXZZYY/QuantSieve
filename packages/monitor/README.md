# quantsieve-monitor

Delayed, source-linked market signal feed.

- selected public RSS aggregation with per-source failure isolation
- official SEC EDGAR 13F events
- SQLite event deduplication and visibility timestamps
- optional OpenAI-compatible one-sentence analysis
- a small `MonitorPlugin` protocol for community extensions

Public aggregators are best effort. Consumers should treat SEC EDGAR as the stable baseline.
