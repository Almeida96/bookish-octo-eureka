"""Local game inventory and rental register, backed by transactional SQLite."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

APPLICATION_ID = 0x47414D45
SCHEMA_VERSION = 1


def label(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")
    value = value.strip()
    if not value or len(value) > maximum or not value.isprintable():
        raise ValueError(f"{name} needs 1–{maximum} printable characters.")
    return value


def key(value: str, name: str = "Game code") -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value
    ):
        raise ValueError(
            f"{name} needs 1–64 letters, digits, dots, underscores or hyphens."
        )
    return value


def number(value: int, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}.")
    return value


def day(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Dates must use YYYY-MM-DD.")
    date.fromisoformat(value)
    return value


def initialize(path: Path) -> dict:
    """Create a new register; an existing file is never replaced or migrated."""
    path = Path(path).resolve()
    with path.open("xb"):
        pass
    connection = None
    try:
        connection = sqlite3.connect(path)
        connection.executescript(f"""
            BEGIN IMMEDIATE;
            PRAGMA application_id = {APPLICATION_ID};
            PRAGMA user_version = {SCHEMA_VERSION};
            CREATE TABLE games (
                code TEXT PRIMARY KEY NOT NULL,
                title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 160),
                copies INTEGER NOT NULL CHECK(copies BETWEEN 0 AND 1000)
            );
            CREATE TABLE rentals (
                id INTEGER PRIMARY KEY,
                request_id TEXT UNIQUE NOT NULL,
                game_code TEXT NOT NULL REFERENCES games(code),
                borrower TEXT NOT NULL CHECK(length(borrower) BETWEEN 1 AND 80),
                checked_out TEXT NOT NULL,
                due TEXT NOT NULL CHECK(due >= checked_out),
                returned TEXT CHECK(returned IS NULL OR returned >= checked_out)
            );
            CREATE INDEX rental_availability ON rentals(game_code) WHERE returned IS NULL;
            CREATE INDEX rental_due ON rentals(due) WHERE returned IS NULL;
            COMMIT;
        """)
    except BaseException:
        if connection is not None:
            connection.close()
            connection = None
        path.unlink()
        raise
    finally:
        if connection is not None:
            connection.close()
    return {"status": "created", "schema_version": SCHEMA_VERSION}


@contextmanager
def database(path: Path, *, write: bool = False):
    """Read operations cannot create a missing file or change its contents."""
    uri = Path(path).resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
    connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError(
                "This is not a game rental register. Use init with a new file."
            )
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Unsupported register schema version.")
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def game_row(connection, code):
    row = connection.execute(
        """
        SELECT g.code, g.title, g.copies,
               count(r.id) AS rented, g.copies-count(r.id) AS available
        FROM games g LEFT JOIN rentals r ON r.game_code=g.code AND r.returned IS NULL
        WHERE g.code=? GROUP BY g.code
    """,
        (code,),
    ).fetchone()
    if row is None:
        raise ValueError("Game code was not found.")
    return dict(row)


def add_game(path, code, title, copies):
    code, title = key(code), label(title, "Title", 160)
    number(copies, "Copies", 0, 1000)
    with database(path, write=True) as connection:
        if connection.execute("SELECT 1 FROM games WHERE code=?", (code,)).fetchone():
            raise ValueError("Game code already exists. Use stock to adjust copies.")
        connection.execute("INSERT INTO games VALUES (?, ?, ?)", (code, title, copies))
        return game_row(connection, code)


def set_stock(path, code, copies):
    key(code)
    number(copies, "Copies", 0, 1000)
    with database(path, write=True) as connection:
        current = game_row(connection, code)
        if copies < current["rented"]:
            raise ValueError("Stock cannot be lower than the number of active rentals.")
        connection.execute("UPDATE games SET copies=? WHERE code=?", (copies, code))
        return game_row(connection, code)


def checkout(path, code, borrower, due, on, request_id):
    key(code)
    borrower = label(borrower, "Borrower alias", 80)
    due, on = day(due), day(on)
    key(request_id, "Request ID")
    if due < on:
        raise ValueError("Due date cannot precede checkout date.")
    with database(path, write=True) as connection:
        existing = connection.execute(
            "SELECT * FROM rentals WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["game_code"],
                existing["borrower"],
                existing["due"],
                existing["checked_out"],
            ) != (code, borrower, due, on):
                raise ValueError("Request ID is already used for a different rental.")
            return {"created": False, "rental": dict(existing)}
        if game_row(connection, code)["available"] <= 0:
            raise ValueError("No copy is available for this game.")
        cursor = connection.execute(
            """
            INSERT INTO rentals(request_id, game_code, borrower, checked_out, due)
            VALUES (?, ?, ?, ?, ?)
        """,
            (request_id, code, borrower, on, due),
        )
        row = connection.execute(
            "SELECT * FROM rentals WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        return {"created": True, "rental": dict(row)}


def return_game(path, rental_id, on):
    number(rental_id, "Rental ID", 1, 2**63 - 1)
    on = day(on)
    with database(path, write=True) as connection:
        row = connection.execute(
            "SELECT * FROM rentals WHERE id=?", (rental_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Rental ID was not found.")
        if on < row["checked_out"]:
            raise ValueError("Return date cannot precede checkout date.")
        if row["returned"] is not None:
            if row["returned"] != on:
                raise ValueError(
                    "This rental was already returned on a different date."
                )
            return {"changed": False, "rental": dict(row)}
        connection.execute("UPDATE rentals SET returned=? WHERE id=?", (on, rental_id))
        return {
            "changed": True,
            "rental": dict(
                connection.execute(
                    "SELECT * FROM rentals WHERE id=?", (rental_id,)
                ).fetchone()
            ),
        }


def pagination(offset, limit):
    number(offset, "Offset", 0, 1_000_000)
    number(limit, "Limit", 1, 200)


def page(rows, total, offset, limit):
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < total else None,
    }


def inventory(path, query="", offset=0, limit=50):
    pagination(offset, limit)
    if (
        not isinstance(query, str)
        or len(query) > 160
        or (query and not query.isprintable())
    ):
        raise ValueError("Search needs at most 160 printable characters.")
    with database(path) as connection:
        where = "instr(lower(code),lower(?))>0 OR instr(lower(title),lower(?))>0"
        total = connection.execute(
            f"SELECT count(*) FROM games WHERE {where}", (query, query)
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT g.code, g.title, g.copies, count(r.id) AS rented,
                   g.copies-count(r.id) AS available
            FROM games g LEFT JOIN rentals r ON r.game_code=g.code AND r.returned IS NULL
            WHERE {where} GROUP BY g.code ORDER BY g.code LIMIT ? OFFSET ?
        """,
            (query, query, limit, offset),
        ).fetchall()
        return page(rows, total, offset, limit)


def list_rentals(path, status="active", as_of=None, borrower=None, offset=0, limit=50):
    pagination(offset, limit)
    as_of = day(date.today().isoformat() if as_of is None else as_of)
    conditions = {
        "all": "1=1",
        "active": "returned IS NULL",
        "returned": "returned IS NOT NULL",
        "overdue": "returned IS NULL AND due < ?",
    }
    if status not in conditions:
        raise ValueError("Status must be all, active, returned or overdue.")
    where, parameters = conditions[status], [as_of] if status == "overdue" else []
    if borrower is not None:
        where += " AND borrower=?"
        parameters.append(label(borrower, "Borrower alias", 80))
    with database(path) as connection:
        total = connection.execute(
            f"SELECT count(*) FROM rentals WHERE {where}", parameters
        ).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM rentals WHERE {where} ORDER BY due,id LIMIT ? OFFSET ?",
            [*parameters, limit, offset],
        ).fetchall()
        return {**page(rows, total, offset, limit), "status": status, "as_of": as_of}


def summary(path, as_of=None):
    as_of = day(date.today().isoformat() if as_of is None else as_of)
    with database(path) as connection:
        games, copies = connection.execute(
            "SELECT count(*), coalesce(sum(copies),0) FROM games"
        ).fetchone()
        active, overdue = connection.execute(
            """
            SELECT count(*), coalesce(sum(due < ?),0) FROM rentals WHERE returned IS NULL
        """,
            (as_of,),
        ).fetchone()
        return {
            "games": games,
            "copies": copies,
            "rented": active,
            "available": copies - active,
            "overdue": overdue,
            "as_of": as_of,
        }


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, json.dumps({"error": message}, ensure_ascii=True) + "\n")


def parser():
    root = JsonArgumentParser(
        description="Track local game stock, rentals and returns."
    )
    root.add_argument(
        "--db",
        type=Path,
        default=Path("rentals.db"),
        help="SQLite register (default: rentals.db)",
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "init", help="Create a new register; never replace an existing file"
    )
    add = commands.add_parser("add", help="Register a game")
    add.add_argument("code")
    add.add_argument("title")
    add.add_argument("copies", type=int)
    stock = commands.add_parser("stock", help="Set total physical copies")
    stock.add_argument("code")
    stock.add_argument("copies", type=int)
    rent = commands.add_parser("rent", help="Check out one available copy")
    rent.add_argument("code")
    rent.add_argument("borrower")
    rent.add_argument("--due", required=True)
    rent.add_argument("--on", default=date.today().isoformat())
    rent.add_argument("--request", required=True, help="Unique retry-safe request ID")
    returned = commands.add_parser("return", help="Return a rental by its ID")
    returned.add_argument("id", type=int)
    returned.add_argument("--on", default=date.today().isoformat())
    games = commands.add_parser("inventory", help="Search available stock")
    games.add_argument("--query", default="")
    rentals = commands.add_parser("rentals", help="List rental records")
    rentals.add_argument(
        "--status", choices=["all", "active", "returned", "overdue"], default="active"
    )
    rentals.add_argument("--borrower")
    for command in (
        rentals,
        commands.add_parser("summary", help="Summarize stock and overdue rentals"),
    ):
        command.add_argument("--as-of", default=date.today().isoformat())
    for command in (games, rentals):
        command.add_argument("--offset", type=int, default=0)
        command.add_argument("--limit", type=int, default=50)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args.db)
        elif args.command == "add":
            result = add_game(args.db, args.code, args.title, args.copies)
        elif args.command == "stock":
            result = set_stock(args.db, args.code, args.copies)
        elif args.command == "rent":
            result = checkout(
                args.db, args.code, args.borrower, args.due, args.on, args.request
            )
        elif args.command == "return":
            result = return_game(args.db, args.id, args.on)
        elif args.command == "inventory":
            result = inventory(args.db, args.query, args.offset, args.limit)
        elif args.command == "rentals":
            result = list_rentals(
                args.db, args.status, args.as_of, args.borrower, args.offset, args.limit
            )
        else:
            result = summary(args.db, args.as_of)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
