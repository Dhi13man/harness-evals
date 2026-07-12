import unittest

from registry import UserRegistry


class FakeCursor:
    def fetchone(self):
        return ("alex@example.com", "Alex")


class FakeConnection:
    def execute(self, _statement, _parameters=()):
        return FakeCursor()

    def commit(self):
        return None


class UserRegistryTests(unittest.TestCase):
    def test_find_returns_a_user(self):
        registry = UserRegistry(FakeConnection())
        registry.add("alex@example.com", "Alex")
        self.assertEqual("Alex", registry.find("alex@example.com")["display_name"])


if __name__ == "__main__":
    unittest.main()
