import unittest

from api.http_range import (
    InvalidRangeHeader,
    UnsatisfiableRange,
    parse_single_range,
)


class HttpRangeTests(unittest.TestCase):
    def test_bounded_range(self):
        result = parse_single_range("bytes=100-199", 1000)
        self.assertEqual((result.start, result.end, result.length), (100, 199, 100))
        self.assertEqual(result.content_range, "bytes 100-199/1000")

    def test_open_ended_range(self):
        result = parse_single_range("bytes=900-", 1000)
        self.assertEqual((result.start, result.end), (900, 999))

    def test_suffix_range(self):
        result = parse_single_range("bytes=-250", 1000)
        self.assertEqual((result.start, result.end), (750, 999))

    def test_end_is_clamped_to_resource(self):
        result = parse_single_range("bytes=950-1200", 1000)
        self.assertEqual((result.start, result.end), (950, 999))

    def test_multiple_ranges_are_rejected(self):
        with self.assertRaises(InvalidRangeHeader):
            parse_single_range("bytes=0-99,200-299", 1000)

    def test_invalid_and_unsatisfiable_ranges_are_distinct(self):
        with self.assertRaises(InvalidRangeHeader):
            parse_single_range("bytes=200-100", 1000)
        with self.assertRaises(UnsatisfiableRange):
            parse_single_range("bytes=1000-", 1000)
        with self.assertRaises(UnsatisfiableRange):
            parse_single_range("bytes=0-", 0)


if __name__ == "__main__":
    unittest.main()
