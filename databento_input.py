"""Normalize a local Databento MBP-10 window into the replay's event rows."""

from calendar import timegm
import re

OUTRIGHT = re.compile(r'NQ[HMUZ][0-9]{1,2}')
UNDISPLAYED_PRICE = (1 << 63) - 1
UNDEFINED_TIMESTAMP = (1 << 64) - 1
TICK_NANODOLLARS = 250_000_000
MICROSECONDS_IN_DAY = 86_400_000_000
EMPTY_BOOK = ((None, {}), (None, {}))


def instrument_id_for(mappings, symbol, day):
    if not OUTRIGHT.fullmatch(symbol):
        raise ValueError('use a Databento NQ outright contract symbol such as NQU6')
    ids = {int(interval['symbol']) for interval in mappings.get(symbol, [])
           if interval['start_date'] <= day < interval['end_date']}
    if len(ids) != 1:
        available = sorted(name for name, intervals in mappings.items()
                           if OUTRIGHT.fullmatch(name)
                           and any(span['start_date'] <= day < span['end_date'] for span in intervals))
        listed = ', '.join(available) or 'none'
        raise ValueError(f'no unique instrument ID for {symbol} on {day}; NQ outrights in the file: {listed}')
    return ids.pop()


def price_ticks(price):
    ticks, remainder = divmod(price, TICK_NANODOLLARS)
    if remainder or not 0 < price < UNDISPLAYED_PRICE:
        raise ValueError(f'invalid NQ price in DBN record: {price}')
    return ticks


def price_text(ticks):
    whole, quarter = divmod(ticks, 4)
    return f'{whole}.{quarter * 25:02d}'


def read_book(levels):
    sides = [{}, {}]
    quotes = [None, None]
    for rank, level in enumerate(levels):
        for side, (price, size) in enumerate(((level.bid_px, level.bid_sz), (level.ask_px, level.ask_sz))):
            if not size:
                continue
            ticks = price_ticks(price)
            if rank == 0:
                quotes[side] = (ticks, size)
            else:
                sides[side][ticks] = (size, rank)
    return tuple(zip(quotes, sides))


def book_updates(stamp, previous, current):
    for side, ((old, _), (new, _)) in enumerate(zip(previous, current)):
        if old != new:
            value, size = new or (0, 0)
            yield f'{stamp};1;{side};{price_text(value)};{size}'

    for side, ((old, old_levels), (new, new_levels)) in enumerate(zip(previous, current)):
        for value, (_, rank) in old_levels.items():
            if value not in new_levels:
                yield f'{stamp};2;{side};{price_text(value)};0;{rank};2'
        touch_moved = (old and old[0]) != (new and new[0])
        for value, (size, rank) in new_levels.items():
            old_level = old_levels.get(value)
            if old_level is None or old_level[0] != size or touch_moved:
                action = int(old_level is not None)
                yield f'{stamp};2;{side};{price_text(value)};{size};{rank};{action}'


def timestamp(prefix, offset):
    seconds, microseconds = divmod(offset, 1_000_000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f'{prefix}{hour:02d}{minute:02d}{second:02d}{microseconds:06d}'


def window_rows(records, instrument, day, start_us, end_us):
    """Yield events and bare timestamps that establish file coverage."""
    midnight_us = timegm(day.timetuple()) * 1_000_000
    prefix = day.strftime('%Y%m%d')
    book = EMPTY_BOOK
    warmup = None
    last_receive = -1
    started = False

    for record in records:
        if record.instrument_id != instrument:
            continue
        receive = record.ts_recv
        if receive == UNDEFINED_TIMESTAMP or receive < last_receive:
            raise ValueError('missing or out-of-order DBN capture timestamp for the selected contract')
        last_receive = receive
        offset = receive // 1000 - midnight_us
        if offset < 0:
            continue
        if offset >= MICROSECONDS_IN_DAY:
            break
        if offset < start_us:
            warmup = (offset, record)
            continue

        stamp = timestamp(prefix, offset)
        if not started:
            started = True
            if warmup is not None:
                offset_before_start, last_record = warmup
                warmup_stamp = timestamp(prefix, offset_before_start)
                book = read_book(last_record.levels)
                yield warmup_stamp
                yield from book_updates(warmup_stamp, EMPTY_BOOK, book)
            else:
                yield stamp
        if offset >= end_us:
            yield stamp
            return

        if str(record.action) == 'T':
            value = price_text(price_ticks(record.price))
            if not record.size:
                raise ValueError('zero-size trade in DBN record')
            yield f'{stamp};1;2;{value};{record.size}'

        next_book = read_book(record.levels)
        yield from book_updates(stamp, book, next_book)
        book = next_book

    if not started and warmup is not None:
        yield timestamp(prefix, warmup[0])


def decode_records(stream, decoder):
    while block := stream.read(64 * 1024):
        yield from decoder.write_and_decode(block)
    yield from decoder.decode()
    if decoder.buffer():
        raise ValueError('DBN file ends with an incomplete record')


def read_rows(source, symbol, day, start_us, end_us):
    try:
        from databento_dbn import Compression, DBNDecoder, DBNError, MBP10Msg, Metadata, VersionUpgradePolicy
    except ImportError as error:
        raise ValueError('Databento input requires: python -m pip install "databento-dbn>=0.70"') from error

    compression = Compression.ZSTD if source.suffix.lower() == '.zst' else Compression.NONE
    try:
        decoder = DBNDecoder(compression=compression, upgrade_policy=VersionUpgradePolicy.UPGRADE_TO_V3)
    except TypeError as error:
        raise ValueError('Update the DBN reader: python -m pip install "databento-dbn>=0.70"') from error

    try:
        with source.open('rb') as stream:
            records = decode_records(stream, decoder)
            metadata = next(records, None)
            if not isinstance(metadata, Metadata) or metadata.dataset != 'GLBX.MDP3' or metadata.schema != 'mbp-10':
                raise ValueError('expected a Databento GLBX.MDP3 file with the mbp-10 schema')
            if metadata.stype_out != 'instrument_id':
                raise ValueError('DBN metadata must map contract symbols to instrument IDs')
            instrument = instrument_id_for(metadata.mappings, symbol, day)
            updates = (record for record in records if isinstance(record, MBP10Msg))
            yield from window_rows(updates, instrument, day, start_us, end_us)
    except DBNError as error:
        raise ValueError(f'could not decode {source.name}: {error}') from error
