import sqlite3
import tempfile
import unittest
from pathlib import Path

from registry import DuplicateUser, UserRegistry


class RegistryContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "registry.sqlite"
        self.connection = sqlite3.Connection(str(self.database_path))
        self.registry = UserRegistry(self.connection)

    def tearDown(self):
        if self.connection is not None:
            self.connection.close()
        self.temporary.cleanup()

    def test_persists_across_connections(self):
        self.registry.add("alex@example.com", "Alex")
        self.connection.close()
        self.connection = None
        reader = sqlite3.Connection(str(self.database_path))
        try:
            assert UserRegistry(reader).find("alex@example.com") == {
                "email": "alex@example.com",
                "display_name": "Alex",
            }
        finally:
            reader.close()

    def test_identity_and_duplicates(self):
        self.registry.add(" Alex@Example.COM ", "Alex")
        found = self.registry.find("alex@example.com")
        assert found is not None
        assert found["display_name"] == "Alex"
        try:
            self.registry.add("ALEX@example.com", "Replacement")
        except DuplicateUser:
            pass
        else:
            assert False, "duplicate accepted"
        assert self.registry.find("alex@example.com")["display_name"] == "Alex"

    def test_missing_and_wildcard_lookup(self):
        self.registry.add("axb@example.com", "Plain")
        assert self.registry.find("missing@example.com") is None
        assert self.registry.find("a_b@example.com") is None
