# Game rental register

[![CI](https://github.com/Almeida96/bookish-octo-eureka/actions/workflows/ci.yml/badge.svg)](https://github.com/Almeida96/bookish-octo-eureka/actions/workflows/ci.yml)

Track a small game collection, check out available copies, record returns, and find overdue rentals. The register persists in a local SQLite file and works with Python 3.11+ without third-party packages.

This repository originally held only the idea for a game-rental program. It now contains a working command-line application and integration tests.

## First rental

From the checkout, create a new register and add a game:

```sh
python rentals.py --db demo.db init
python rentals.py --db demo.db add GAME-001 "Example board game" 2
python rentals.py --db demo.db rent GAME-001 player-a --due 2026-11-17 --on 2026-11-10 --request checkout-001
python rentals.py --db demo.db inventory
```

The rental response includes its integer `id`. Use that ID to return the copy:

```sh
python rentals.py --db demo.db return 1 --on 2026-11-12
python rentals.py --db demo.db rentals --status returned
python rentals.py --db demo.db summary --as-of 2026-11-18
```

The example dates make the sequence reproducible. For everyday use, omit `--on` to use your computer's local date and choose the intended due date. A rental becomes overdue the day **after** its due date. A late return remains in history but no longer counts as an active overdue rental. `--as-of` changes the overdue cutoff; it does not reconstruct past stock or rental status.

## Commands

Place `--db PATH` before the command. The default file is `rentals.db` in the current directory. Every successful command writes JSON to stdout; invalid input or a storage failure writes a JSON error to stderr and exits with code 2.

| Command | Purpose |
| --- | --- |
| `init` | Create a new register; existing files are never overwritten. |
| `add CODE TITLE COPIES` | Register a game with 0–1,000 physical copies. |
| `stock CODE COPIES` | Adjust total copies, never below the active rental count. |
| `rent CODE BORROWER --due DATE --request ID [--on DATE]` | Check out one available copy. |
| `return ID [--on DATE]` | Record a return by rental ID. |
| `inventory [--query TEXT]` | Search codes/titles and inspect total, rented and available copies. |
| `rentals [--status STATUS] [--borrower ALIAS] [--as-of DATE]` | Inspect rental history; status is active (default), returned, overdue or all. |
| `summary [--as-of DATE]` | Count games, copies, active rentals and overdue rentals. |

Inventory and rental lists accept `--offset` and `--limit` (default 50, maximum 200). Follow `next_offset` until it is `null`. Search is a literal substring; `%` and `_` are ordinary characters. SQLite's built-in case-insensitive matching handles ASCII case, not full Unicode case folding. Borrower filters are exact and case-sensitive.

Game codes and request IDs use 1–64 ASCII letters, digits, dots, underscores or hyphens, beginning with a letter or digit. Titles accept 160 printable characters; borrower aliases accept 80. Use aliases rather than contact details when demonstrating or sharing the register.

## Stock and retry behavior

A checkout, stock adjustment or return runs in a SQLite write transaction. Concurrent processes cannot allocate the same last copy; a busy register waits up to five seconds before returning an error.

Choose a distinct request ID for each intended checkout. Retrying the same ID with identical game, borrower and dates returns the existing record without renting another copy. Changing those details under the same ID is rejected. Retrying a checkout after its rental has been returned still returns that original rental; use a new request ID for a new checkout.

Repeating a return with the same date is harmless. A different return date for an already returned rental is rejected. Neither return path increases the recorded physical stock.

## Storage and scope

Read commands open SQLite in read-only mode and do not create missing registers. Existing files must have this application's schema marker; unrelated databases are rejected. The first release has schema version 1 and no migration mechanism.

The application tracks interchangeable copies per game code. It does not track individual serial numbers, reservations, rental prices, payments, damage, accounts or remote access. There is no authentication or encryption. SQLite transactions protect normal concurrent writes, but a file in a synced/network folder is not a substitute for a database server. Keep the live register on a local disk and back it up while no application process has it open.

## Development

```sh
python -m unittest discover -s tests -v
```

Tests use temporary databases. They cover real CLI subprocesses, persistence, overdue boundaries, stock limits, concurrent checkout, duplicate requests and returns, invalid inputs, pagination, and rejection of unrelated databases. CI runs the suite on Linux and Windows with Python 3.11 and 3.14.

To contribute, include a regression test for behavior changes. Do not commit live databases or borrower information. No source-code license has been declared.
