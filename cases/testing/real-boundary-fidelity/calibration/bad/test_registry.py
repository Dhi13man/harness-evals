import unittest

from registry import UserRegistry


class Cursor:
    def fetchone(self):
        return ("alex@example.com", "Alex")


class FakeConnection:
    def execute(self, _query, _parameters=()):
        return Cursor()

    def commit(self):
        return None


class UserRegistryTests(unittest.TestCase):
    def test_fake_round_trip(self):
        registry = UserRegistry(FakeConnection())
        registry.add("alex@example.com", "Alex")
        self.assertEqual("Alex", registry.find("alex@example.com")["display_name"])


if __name__ == "__main__":
    unittest.main()
