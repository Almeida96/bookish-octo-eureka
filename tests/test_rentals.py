import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import rentals


class RentalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "register.db"
        rentals.initialize(self.path)
        rentals.add_game(self.path, "G-1", "Example game", 1)

    def rent(self, request="order-1", borrower="player-a"):
        return rentals.checkout(
            self.path, "G-1", borrower, "2026-11-17", "2026-11-10", request
        )

    def test_rental_and_return_restore_availability(self):
        result = self.rent()
        self.assertEqual(rentals.inventory(self.path)["items"][0]["available"], 0)
        self.assertRaisesRegex(ValueError, "No copy", self.rent, "order-2")
        returned = rentals.return_game(self.path, result["rental"]["id"], "2026-11-12")
        self.assertTrue(returned["changed"])
        self.assertEqual(rentals.inventory(self.path)["items"][0]["available"], 1)
        self.assertTrue(self.rent("order-2")["created"])

    def test_checkout_retries_are_idempotent_and_conflicts_are_rejected(self):
        first = self.rent()
        retry = self.rent()
        self.assertFalse(retry["created"])
        self.assertEqual(first["rental"], retry["rental"])
        self.assertRaisesRegex(
            ValueError, "different rental", self.rent, "order-1", "player-b"
        )
        rentals.return_game(self.path, first["rental"]["id"], "2026-11-12")
        retry_after_return = self.rent()
        self.assertFalse(retry_after_return["created"])
        self.assertEqual(retry_after_return["rental"]["returned"], "2026-11-12")
        self.assertEqual(rentals.summary(self.path)["rented"], 0)

    def test_duplicate_returns_do_not_increase_stock(self):
        rental_id = self.rent()["rental"]["id"]
        rentals.return_game(self.path, rental_id, "2026-11-11")
        self.assertFalse(
            rentals.return_game(self.path, rental_id, "2026-11-11")["changed"]
        )
        self.assertRaisesRegex(
            ValueError,
            "already returned",
            rentals.return_game,
            self.path,
            rental_id,
            "2026-11-12",
        )
        self.assertEqual(rentals.summary(self.path)["available"], 1)

    def test_stock_cannot_drop_below_active_rentals(self):
        rental_id = self.rent()["rental"]["id"]
        self.assertRaisesRegex(
            ValueError, "active rentals", rentals.set_stock, self.path, "G-1", 0
        )
        self.assertEqual(rentals.set_stock(self.path, "G-1", 2)["available"], 1)
        rentals.return_game(self.path, rental_id, "2026-11-11")
        self.assertEqual(rentals.set_stock(self.path, "G-1", 0)["available"], 0)

    def test_overdue_boundary_and_borrower_filter(self):
        self.rent()
        self.assertEqual(
            rentals.list_rentals(self.path, "overdue", "2026-11-17")["total"], 0
        )
        self.assertEqual(
            rentals.list_rentals(self.path, "overdue", "2026-11-18")["total"], 1
        )
        self.assertEqual(rentals.summary(self.path, "2026-11-18")["overdue"], 1)
        self.assertEqual(
            rentals.list_rentals(self.path, borrower="player-b")["total"], 0
        )

    def test_pagination_and_literal_search(self):
        rentals.add_game(self.path, "G-2", "100% Quest", 2)
        rentals.add_game(self.path, "G-3", "Other game", 0)
        first = rentals.inventory(self.path, limit=2)
        self.assertEqual(first["next_offset"], 2)
        second = rentals.inventory(self.path, offset=first["next_offset"], limit=2)
        self.assertIsNone(second["next_offset"])
        self.assertEqual(
            [r["code"] for r in first["items"] + second["items"]], ["G-1", "G-2", "G-3"]
        )
        self.assertEqual(rentals.inventory(self.path, query="%")["total"], 1)
        self.assertEqual(rentals.inventory(self.path, query="GAME")["total"], 2)

    def test_reads_and_failed_mutations_preserve_database(self):
        before = self.path.read_bytes()
        rentals.inventory(self.path)
        rentals.list_rentals(self.path)
        rentals.summary(self.path)
        self.assertRaises(
            ValueError, rentals.add_game, self.path, "G-1", "Duplicate", 1
        )
        self.assertRaises(ValueError, rentals.set_stock, self.path, "missing", 1)
        self.assertEqual(before, self.path.read_bytes())

    def test_initialization_never_replaces_existing_files(self):
        before = self.path.read_bytes()
        self.assertRaises(FileExistsError, rentals.initialize, self.path)
        self.assertEqual(before, self.path.read_bytes())
        missing = self.path.with_name("missing.db")
        self.assertRaises(sqlite3.Error, rentals.summary, missing)
        self.assertFalse(missing.exists())

    def test_foreign_or_newer_database_is_not_modified(self):
        foreign = self.path.with_name("foreign.db")
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
        before = foreign.read_bytes()
        self.assertRaisesRegex(
            ValueError, "not a game", rentals.add_game, foreign, "G-1", "Example", 1
        )
        self.assertEqual(before, foreign.read_bytes())
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version=999")
            connection.commit()
        self.assertRaisesRegex(ValueError, "schema version", rentals.summary, self.path)

    def test_invalid_data_does_not_mutate_register(self):
        before = self.path.read_bytes()
        for code, title, count in [
            ("bad/code", "Example", 1),
            ("ok", "\n", 1),
            ("ok", "x" * 161, 1),
            ("ok", "Game", True),
            ("ok", "Game", -1),
        ]:
            with self.subTest(code=code, title=title, count=count):
                self.assertRaises(
                    ValueError, rentals.add_game, self.path, code, title, count
                )
        self.assertRaises(
            ValueError,
            rentals.checkout,
            self.path,
            "G-1",
            "player",
            "2026-11-09",
            "2026-11-10",
            "r",
        )
        self.assertRaises(
            ValueError,
            rentals.checkout,
            self.path,
            "G-1",
            "player",
            "2026-02-30",
            "2026-11-10",
            "r",
        )
        self.assertRaises(ValueError, rentals.inventory, self.path, limit=0)
        self.assertRaises(ValueError, rentals.inventory, self.path, offset=-1)
        self.assertEqual(before, self.path.read_bytes())

    def test_return_cannot_precede_checkout(self):
        rental_id = self.rent()["rental"]["id"]
        self.assertRaises(
            ValueError, rentals.return_game, self.path, rental_id, "2026-11-09"
        )
        self.assertEqual(rentals.summary(self.path)["rented"], 1)

    def test_empty_as_of_date_is_rejected(self):
        self.assertRaises(ValueError, rentals.summary, self.path, "")
        self.assertRaises(ValueError, rentals.list_rentals, self.path, as_of="")

    def test_cli_syntax_errors_are_json_and_do_not_create_a_register(self):
        missing = self.path.with_name("missing.db")
        result = subprocess.run(
            [
                sys.executable,
                str(Path(rentals.__file__).resolve()),
                "--db",
                str(missing),
                "rent",
                "G-1",
                "player",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--due", json.loads(result.stderr)["error"])
        self.assertEqual(result.stdout, "")
        self.assertFalse(missing.exists())

    def test_concurrent_checkout_never_oversells(self):
        def attempt(index):
            try:
                return self.rent(f"concurrent-{index}")["created"]
            except ValueError as error:
                self.assertIn("No copy", str(error))
                return False

        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(attempt, range(4)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(rentals.summary(self.path)["rented"], 1)

    def test_concurrent_identical_request_is_only_inserted_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.rent("same-request"), range(4)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(len({result["rental"]["id"] for result in results}), 1)

    def test_sql_looking_borrower_is_plain_data(self):
        result = self.rent(borrower="'); DROP TABLE games; --")
        self.assertEqual(result["rental"]["borrower"], "'); DROP TABLE games; --")
        self.assertEqual(rentals.inventory(self.path)["total"], 1)

    def test_cli_roundtrip_across_processes(self):
        script = Path(rentals.__file__).resolve()

        def run(*args, success=True):
            result = subprocess.run(
                [sys.executable, str(script), "--db", str(self.path), *args],
                cwd=self.temporary.name,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0 if success else 2, result.stderr)
            return json.loads(result.stdout if success else result.stderr)

        row = run(
            "rent",
            "G-1",
            "player",
            "--due",
            "2026-11-17",
            "--on",
            "2026-11-10",
            "--request",
            "cli-1",
        )
        self.assertEqual(run("inventory")["items"][0]["available"], 0)
        self.assertIn(
            "No copy",
            run(
                "rent",
                "G-1",
                "other",
                "--due",
                "2026-11-17",
                "--on",
                "2026-11-10",
                "--request",
                "cli-2",
                success=False,
            )["error"],
        )
        run("return", str(row["rental"]["id"]), "--on", "2026-11-12")
        self.assertEqual(run("rentals", "--status", "returned")["total"], 1)
        self.assertEqual(run("summary")["available"], 1)


if __name__ == "__main__":
    unittest.main()
