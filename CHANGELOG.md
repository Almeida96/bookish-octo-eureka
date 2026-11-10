# Changelog

## 0.1.0 — 2026-11-10

- Implement game inventory, stock adjustments, checkout and return commands.
- Persist records in transactional SQLite, with read-only queries and explicit initialization.
- Prevent duplicate checkout requests, duplicate returns and concurrent stock overselling.
- Add overdue/history queries, pagination, JSON output and Windows/Linux CI.
