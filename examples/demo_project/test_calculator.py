import unittest

from calculator import mean


class MeanTests(unittest.TestCase):
    def test_three_values(self) -> None:
        self.assertEqual(mean([2, 4, 6]), 4)

    def test_one_value(self) -> None:
        self.assertEqual(mean([7]), 7)

    def test_empty_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            mean([])


if __name__ == "__main__":
    unittest.main()

