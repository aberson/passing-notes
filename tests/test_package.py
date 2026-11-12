import unittest

import passing_notes


class PackageTest(unittest.TestCase):
    def test_version_is_declared(self) -> None:
        self.assertEqual(passing_notes.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()
