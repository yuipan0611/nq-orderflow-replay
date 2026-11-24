from calendar import timegm
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from databento_input import instrument_id_for
import replay

try:
    import databento_dbn as dbn
    import zstandard
except ImportError:
    dbn = None


DAY = date(2026, 1, 6)
START_NS = (timegm(DAY.timetuple()) + 10 * 3600) * 1_000_000_000
MAPPINGS = {
    'NQH6': [{'start_date': DAY, 'end_date': DAY + timedelta(days=1), 'symbol': '42'}],
    'NQM6': [{'start_date': DAY, 'end_date': DAY + timedelta(days=1), 'symbol': '99'}],
}


class InstrumentMappingTests(unittest.TestCase):
    def test_contract_mapping_uses_the_selected_utc_date(self):
        self.assertEqual(instrument_id_for(MAPPINGS, 'NQH6', DAY), 42)
        with self.assertRaisesRegex(ValueError, 'NQH6, NQM6'):
            instrument_id_for(MAPPINGS, 'NQU6', DAY)
        with self.assertRaisesRegex(ValueError, 'outright'):
            instrument_id_for(MAPPINGS, 'NQH6-NQM6', DAY)
        with self.assertRaisesRegex(ValueError, 'none'):
            instrument_id_for(MAPPINGS, 'NQH6', DAY + timedelta(days=1))


@unittest.skipUnless(dbn is not None, 'install databento-dbn and zstandard for compressed DBN integration tests')
class CompressedDbnTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'synthetic-fixture.dbn.zst'

    def write_fixture(self, schema):
        def pair(bid_tick, ask_tick, bid_size, ask_size):
            bid = bid_tick * 250_000_000 if bid_tick else dbn.UNDEF_PRICE
            ask = ask_tick * 250_000_000 if ask_tick else dbn.UNDEF_PRICE
            return dbn.BidAskPair(bid_px=bid, ask_px=ask, bid_sz=bid_size, ask_sz=ask_size)

        def record(offset_ns, action, levels, price=80001, size=1, instrument=42):
            return dbn.MBP10Msg(
                publisher_id=1, instrument_id=instrument, ts_event=START_NS + offset_ns - 2000,
                price=price * 250_000_000, size=size, action=action, side=dbn.Side.NONE, depth=0,
                ts_recv=START_NS + offset_ns, levels=levels + [dbn.BidAskPair() for _ in range(10 - len(levels))],
            )

        book = [pair(80000, 80001, 11, 13), pair(79999, 80002, 17, 19)]
        missing_ask = [pair(80000, 0, 11, 0), pair(79999, 0, 17, 0)]
        restored = [pair(79999, 80001, 17, 25), pair(79998, 80002, 21, 29)]
        wrong_contract = [pair(82000, 82001, 98, 99)]
        metadata = dbn.Metadata(
            dataset='GLBX.MDP3', start=START_NS - 1_000_000_000,
            stype_in=dbn.SType.RAW_SYMBOL, stype_out=dbn.SType.INSTRUMENT_ID, schema=schema,
            symbols=list(MAPPINGS),
            mappings=[SimpleNamespace(raw_symbol=symbol, intervals=[SimpleNamespace(**span) for span in spans])
                      for symbol, spans in MAPPINGS.items()],
        )
        data = [
            record(-1_000_000_000, dbn.Action.ADD, book),
            record(-1_000, dbn.Action.ADD, wrong_contract, instrument=99),
            record(0, dbn.Action.TRADE, book, size=3),
            record(100_000_000, dbn.Action.CLEAR, missing_ask),
            record(200_000_000, dbn.Action.ADD, restored),
            record(300_000_000, dbn.Action.TRADE, restored, price=79999, size=2),
            record(2_000_000_000, dbn.Action.TRADE, restored, size=99),
        ]
        self.path.write_bytes(zstandard.ZstdCompressor().compress(bytes(metadata) + b''.join(map(bytes, data))))

    def test_window_uses_capture_time_and_reconstructs_the_selected_book(self):
        self.write_fixture(dbn.Schema.MBP_10)
        data = replay.build(self.path, DAY, '10:00:00', '10:00:02', symbol='nqh6')
        self.assertEqual((data['symbol'], data['timeZone']), ('NQH6', 'UTC'))
        original = [80000, 11, 80001, 13, [[79999, 17, 0], [80002, 19, 1]]]
        restored = [79999, 17, 80001, 25, [[79998, 21, 0], [80002, 29, 1]]]
        self.assertEqual(data['initial'], original)
        self.assertEqual(data['frames'][:3], [original, None, restored])
        self.assertEqual(data['trades'], [[0, 80001, 3, 1], [300, 79999, 2, -1]])
        self.assertEqual(data['checks']['volume'], 5)

    def test_requires_source_coverage_through_the_end(self):
        self.write_fixture(dbn.Schema.MBP_10)
        with self.assertRaisesRegex(ValueError, 'no event at or after'):
            replay.build(self.path, DAY, '10:00:00', '10:00:03', symbol='NQH6')

    def test_rejects_an_incompatible_schema(self):
        self.write_fixture(dbn.Schema.MBP_1)
        with self.assertRaisesRegex(ValueError, 'mbp-10 schema'):
            replay.build(self.path, DAY, '10:00:00', '10:00:02', symbol='NQH6')


if __name__ == '__main__':
    unittest.main()
