from datetime import date
import gzip
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import replay


DAY = date(2026, 1, 7)
ROWS = b"""20260106120000000000;1;0;20599.25;99
20260106120000000000;1;1;20599.50;99
20260107095959000000;1;0;20600.00;12
20260107095959000000;1;1;20600.25;14
20260107095959500000;2;0;20599.75;21;1;0
20260107100000000000;1;2;20600.25;3
20260107100000100000;2;0;20599.75;27;1;1
20260107100001000000;1;2;20600.00;2
20260107100002000000;1;2;20600.25;99
20260108000000000000;1;1;20600.50;99
"""


class ReplayWindowTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)

    def test_plain_and_gzip_share_window_boundaries_and_opening_book(self):
        plain = self.path / 'input.csv'
        compressed = self.path / 'input.csv.gz'
        plain.write_bytes(ROWS)
        compressed.write_bytes(gzip.compress(ROWS))
        data = replay.build(plain, DAY, '10:00:00', '10:00:02')
        self.assertEqual(data['date'], '2026-01-07')
        self.assertEqual(data['windowStart'], '10:00:00')
        self.assertEqual(data['duration'], 2000)
        self.assertEqual(len(data['frames']), 20)
        self.assertEqual(data['initial'], [82400, 12, 82401, 14, [[82399, 21, 0]]])
        self.assertEqual(data['frames'][0][4], [[82399, 21, 0]])
        self.assertEqual(data['frames'][1][4], [[82399, 27, 0]])
        self.assertEqual(data['trades'], [[0, 82401, 3, 1], [1000, 82400, 2, -1]])
        gz_data = replay.build(compressed, DAY, '10:00:00', '10:00:02')
        self.assertEqual(gz_data, dict(data, source=compressed.name))

    def test_other_dates_cannot_supply_missing_window_coverage(self):
        source = self.path / 'input.csv'
        source.write_bytes(ROWS)
        with self.assertRaisesRegex(ValueError, 'no event at or after'):
            replay.build(source, DAY, '10:00:00', '10:00:03')
        with self.assertRaisesRegex(ValueError, 'no events for 2026-01-09'):
            replay.build(source, date(2026, 1, 9), '10:00:00', '10:00:02')
        with self.assertRaisesRegex(ValueError, 'starts after'):
            replay.build(source, DAY, '09:59:58', '10:00:02')

    def test_window_must_be_ordered_and_within_five_minutes(self):
        missing = self.path / 'missing.csv'
        for start, end in [('10:00:02', '10:00:00'), ('10:00:00', '10:05:01')]:
            with self.subTest(start=start, end=end), self.assertRaisesRegex(ValueError, 'within 300 seconds'):
                replay.build(missing, DAY, start, end)


class SampleDownloadTests(unittest.TestCase):
    def test_cache_reuse_and_verified_replacement(self):
        content = gzip.compress(ROWS)
        sample = {'file': 'sample.csv.gz', 'size': len(content), 'sha256': sha256(content).hexdigest()}
        with TemporaryDirectory() as directory, patch.object(replay, 'CACHE_DIR', Path(directory)):
            with patch.object(replay, 'urlopen', return_value=BytesIO(content)) as download:
                cached = replay.fetch_sample('test-v1', sample)
                self.assertEqual(cached.read_bytes(), content)
                self.assertEqual(replay.fetch_sample('test-v1', sample), cached)
                download.assert_called_once()
            corrupt = bytes(len(content))
            cached.write_bytes(corrupt)
            with patch.object(replay, 'urlopen', return_value=BytesIO(corrupt)):
                with self.assertRaisesRegex(ValueError, 'SHA-256'):
                    replay.fetch_sample('test-v1', sample)
                self.assertEqual(cached.read_bytes(), corrupt)
            with patch.object(replay, 'urlopen', return_value=BytesIO(content)):
                self.assertEqual(replay.fetch_sample('test-v1', sample).read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
