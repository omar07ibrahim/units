from __future__ import annotations

import unittest

import unitsentinel


class PackageIdentityTests(unittest.TestCase):
    def test_version_and_public_surface_are_explicit(self) -> None:
        self.assertEqual(unitsentinel.__version__, "0.1.0")
        self.assertEqual(unitsentinel.__all__, ["__version__"])


if __name__ == "__main__":
    unittest.main()
