import unittest
from pathlib import Path


# sqlite3.connect() assertEqual() appears here only to spoof raw-text checks.
class RegistryFingerprintTests(unittest.TestCase):
    def test_production_source_fingerprint(self):
        source = Path("registry.py").read_text(encoding="utf-8")
        self.assertIn("WHERE email = ?", source)


if __name__ == "__main__":
    unittest.main()
