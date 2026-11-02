"""Tests for gates/store.py: loading, atomic saving, locking and the shape of a new item."""

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gates import machine, store

ITEM_KEYS = [
    "id", "title", "state", "contract", "owns", "dispatched_to", "dispatched_at", "pr", "review_cycles",
    "verdicts", "merge_approved_head", "merge_approved_at", "escalation", "history",
]
AT = "2026-11-02T18:27:37Z"


def add_item(current: dict, **overrides) -> dict:
    """Add a valid item to `current`, with `overrides` replacing the default arguments."""
    arguments = dict(title="Check links", contract="contracts/T-001.md", owns=["src/**"], actor="planner", at=AT)
    arguments.update(overrides)
    return store.new_item(current, **arguments)


class TempDirTestCase(unittest.TestCase):
    """Gives each test a temporary directory and a store path inside it."""

    def setUp(self) -> None:
        """Create the directory and remove it when the test ends."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / "gates.json"


class LoadTests(TempDirTestCase):
    """Reading the store file."""

    def test_a_missing_file_loads_as_an_empty_store(self) -> None:
        self.assertEqual(store.load(self.path), {"version": 1, "items": {}})

    def test_a_saved_store_keeps_the_item_shape_and_key_order(self) -> None:
        saved = add_item(store.empty_store())
        store.save(self.path, saved)
        loaded = store.load(self.path)
        self.assertEqual(loaded, saved)
        self.assertEqual(list(loaded["items"]["T-001"]), ITEM_KEYS)

    def test_the_file_is_indented_with_a_trailing_newline_and_keeps_non_ascii_text(self) -> None:
        store.save(self.path, add_item(store.empty_store(), title="Vérifier les liens"))
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("}\n"))
        self.assertIn('\n  "version": 1', text)
        self.assertIn("Vérifier les liens", text)

    def test_items_keep_their_insertion_order(self) -> None:
        current = add_item(store.empty_store(), item_id="T-010")
        current = add_item(current, item_id="T-002")
        store.save(self.path, current)
        self.assertEqual(list(store.load(self.path)["items"]), ["T-010", "T-002"])

    def test_invalid_json_is_refused(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(store.StoreError):
            store.load(self.path)

    def test_a_file_that_is_not_an_object_is_refused(self) -> None:
        self.path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(store.StoreError):
            store.load(self.path)

    def test_a_wrong_version_is_refused(self) -> None:
        self.path.write_text(json.dumps({"version": 2, "items": {}}), encoding="utf-8")
        with self.assertRaises(store.StoreError):
            store.load(self.path)

    def test_items_that_are_not_an_object_are_refused(self) -> None:
        self.path.write_text(json.dumps({"version": 1, "items": []}), encoding="utf-8")
        with self.assertRaises(store.StoreError):
            store.load(self.path)

    def test_a_store_error_names_the_file(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(store.StoreError) as raised:
            store.load(self.path)
        self.assertIn(str(self.path), str(raised.exception))

    def test_a_store_error_is_a_gate_error(self) -> None:
        self.assertTrue(issubclass(store.StoreError, machine.GateError))


class SaveTests(TempDirTestCase):
    """Writing the store file."""

    def test_save_creates_a_missing_parent_directory(self) -> None:
        path = self.directory / "nested" / "gates.json"
        store.save(path, store.empty_store())
        self.assertEqual(store.load(path), store.empty_store())

    def test_a_failure_while_writing_leaves_the_old_file_and_no_temp_file(self) -> None:
        store.save(self.path, store.empty_store())
        before = self.path.read_text(encoding="utf-8")
        with mock.patch("gates.store.json.dumps", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.save(self.path, add_item(store.empty_store()))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual([p.name for p in self.directory.iterdir()], ["gates.json"])

    def test_a_failure_while_replacing_leaves_the_old_file_and_no_temp_file(self) -> None:
        store.save(self.path, store.empty_store())
        before = self.path.read_text(encoding="utf-8")
        with mock.patch("gates.store.os.replace", side_effect=OSError("cannot rename")):
            with self.assertRaises(OSError):
                store.save(self.path, add_item(store.empty_store()))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual([p.name for p in self.directory.iterdir()], ["gates.json"])


class LockedTests(TempDirTestCase):
    """Reading and writing under the exclusive lock."""

    def test_locked_saves_the_store_on_a_clean_exit(self) -> None:
        with store.locked(self.path) as data:
            data.update(add_item(data))
        self.assertIn("T-001", store.load(self.path)["items"])

    def test_locked_saves_nothing_when_the_block_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            with store.locked(self.path) as data:
                data.update(add_item(data))
                raise RuntimeError("stop")
        self.assertFalse(self.path.exists())

    def test_locked_leaves_the_lock_file_in_place(self) -> None:
        with store.locked(self.path):
            pass
        self.assertTrue(self.path.with_name("gates.json.lock").exists())

    def test_a_second_locked_on_the_same_path_times_out_while_the_first_is_held(self) -> None:
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with store.locked(self.path):
                holding.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        self.assertTrue(holding.wait(timeout=5))
        with self.assertRaises(store.StoreError) as raised:
            with store.locked(self.path, timeout=0.2):
                self.fail("the second lock must not be granted")
        self.assertIn("lock", str(raised.exception))

    def test_the_lock_is_released_after_an_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with store.locked(self.path):
                raise RuntimeError("stop")
        with store.locked(self.path, timeout=0.2):
            pass

    def test_the_lock_is_released_after_a_clean_exit(self) -> None:
        with store.locked(self.path):
            pass
        with store.locked(self.path, timeout=0.2):
            pass


class NewItemTests(unittest.TestCase):
    """Adding a task to a store."""

    def test_the_first_id_is_t_001(self) -> None:
        self.assertIn("T-001", add_item(store.empty_store())["items"])

    def test_the_next_id_is_one_above_the_highest_existing_id(self) -> None:
        current = add_item(store.empty_store(), item_id="T-007")
        current = add_item(current, item_id="T-003")
        self.assertIn("T-008", add_item(current)["items"])

    def test_ids_are_numbered_with_at_least_three_digits(self) -> None:
        current = add_item(store.empty_store(), item_id="T-1000")
        self.assertIn("T-1001", add_item(current)["items"])

    def test_a_given_id_is_used(self) -> None:
        self.assertIn("T-042", add_item(store.empty_store(), item_id="T-042")["items"])

    def test_a_new_item_has_the_item_shape_in_order(self) -> None:
        item = add_item(store.empty_store())["items"]["T-001"]
        self.assertEqual(list(item), ITEM_KEYS)
        self.assertEqual(item["state"], "draft")
        self.assertEqual(item["owns"], ["src/**"])
        self.assertEqual(item["contract"], "contracts/T-001.md")
        self.assertEqual(item["review_cycles"], 0)
        self.assertEqual(item["verdicts"], [])
        self.assertIsNone(item["escalation"])

    def test_the_first_history_event_records_the_creation(self) -> None:
        item = add_item(store.empty_store(), actor="planner")["items"]["T-001"]
        self.assertEqual(
            item["history"],
            [{"at": AT, "from": None, "to": "draft", "actor": "planner", "reason": "created"}],
        )

    def test_a_duplicate_id_is_refused(self) -> None:
        current = add_item(store.empty_store())
        with self.assertRaises(store.StoreError):
            add_item(current, item_id="T-001")

    def test_an_id_that_does_not_match_the_pattern_is_refused(self) -> None:
        for bad_id in ("T-1", "T-12", "X-001", "T-00a", "t-001", ""):
            with self.subTest(item_id=bad_id):
                with self.assertRaises(store.StoreError):
                    add_item(store.empty_store(), item_id=bad_id)

    def test_an_empty_title_is_refused(self) -> None:
        for title in ("", "   "):
            with self.subTest(title=title):
                with self.assertRaises(store.StoreError):
                    add_item(store.empty_store(), title=title)

    def test_an_empty_contract_path_is_refused(self) -> None:
        with self.assertRaises(store.StoreError):
            add_item(store.empty_store(), contract="")

    def test_an_empty_owns_list_is_refused(self) -> None:
        with self.assertRaises(store.StoreError):
            add_item(store.empty_store(), owns=[])

    def test_an_empty_glob_in_owns_is_refused(self) -> None:
        with self.assertRaises(store.StoreError):
            add_item(store.empty_store(), owns=["src/**", ""])

    def test_new_item_does_not_change_its_input(self) -> None:
        current = add_item(store.empty_store())
        before = copy.deepcopy(current)
        owns = ["lib/**"]
        updated = add_item(current, owns=owns)
        self.assertEqual(current, before)
        self.assertIsNot(updated, current)
        owns.append("other/**")
        self.assertEqual(updated["items"]["T-002"]["owns"], ["lib/**"])


class HelperTests(unittest.TestCase):
    """Small helpers."""

    def test_now_iso_is_utc_to_the_second_with_a_z_suffix(self) -> None:
        self.assertRegex(store.now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_the_default_store_path_is_gates_json_at_the_repository_root(self) -> None:
        path = store.default_store_path()
        self.assertEqual(path.name, "gates.json")
        self.assertTrue((path.parent / "gates").is_dir())

    def test_the_empty_store_has_version_1_and_no_items(self) -> None:
        self.assertEqual(store.empty_store(), {"version": 1, "items": {}})


if __name__ == "__main__":
    unittest.main()
