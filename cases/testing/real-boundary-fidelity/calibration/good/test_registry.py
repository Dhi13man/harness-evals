import sqlite3
import tempfile
import unittest
from pathlib import Path

from registry import DuplicateUser, UserRegistry


class UserRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "users.sqlite"
        self.connection = sqlite3.connect(self.database_path)
        self.registry = UserRegistry(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_added_user_is_durable_for_a_new_connection(self):
        self.registry.add("alex@example.com", "Alex")
        reader_connection = sqlite3.connect(self.database_path)
        self.addCleanup(reader_connection.close)
        persisted = UserRegistry(reader_connection).find("alex@example.com")
        self.assertEqual(
            {"email": "alex@example.com", "display_name": "Alex"}, persisted
        )

    def test_email_identity_is_trimmed_and_case_insensitive(self):
        self.registry.add(" Alex@Example.COM ", "Alex")
        found = self.registry.find("alex@example.com")
        self.assertIsNotNone(found)
        self.assertEqual("Alex", found["display_name"])
        with self.assertRaises(DuplicateUser):
            self.registry.add("ALEX@example.com", "Replacement")
        self.assertEqual("Alex", self.registry.find("alex@example.com")["display_name"])

    def test_missing_user_is_observable(self):
        self.assertIsNone(self.registry.find("missing@example.com"))

    def test_sql_wildcards_are_literal_email_characters(self):
        self.registry.add("axb@example.com", "Plain")
        self.assertIsNone(self.registry.find("a_b@example.com"))


if __name__ == "__main__":
    unittest.main()
