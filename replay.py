"""Build a standalone NQ order flow replay from a local CSV or a synthetic sample."""
import argparse
from collections import Counter
from datetime import date, time
from decimal import Decimal, InvalidOperation
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

SOURCE = Path.home() / 'Desktop/NQ_L2/NQ_L2_2026-01/01/20260105.csv'
DEFAULT_DATE = date(2026, 1, 5)
DEFAULT_START = '05:00:02'
DEFAULT_END = '05:01:02'
MAX_CLIP_SECONDS = 300
STEP_MS = 100
RADIUS_TICKS = 9
HERE = Path(__file__).resolve().parent
CACHE_DIR = Path.home() / '.cache' / 'nq-orderflow-replay'
RELEASES = 'https://github.com/yuipan0611/nq-orderflow-replay/releases/download'



class Book:
    def __init__(self):
        self.quote = [None, None]
        self.depth = [{}, {}]

    def distance(self, side, price):
        touch = self.quote[side]
        if touch is None:
            return None
        return (touch[0] - price) * (1 if side == 0 else -1)

    def set_quote(self, side, price, size):
        if self.quote[side] is None or self.quote[side][0] != price:
            self.quote[side] = (price, size)
            for seen in tuple(self.depth[side]):
                if not 1 <= self.distance(side, seen) <= RADIUS_TICKS:
                    del self.depth[side][seen]
        else:
            self.quote[side] = (price, size)

    def set_depth(self, side, price, size, action):
        levels = self.depth[side]
        distance = self.distance(side, price)
        if action == 2 or size == 0:
            levels.pop(price, None)
        elif distance is None or 1 <= distance <= RADIUS_TICKS:
            levels[price] = size

    def snapshot(self):
        bid, ask = self.quote
        if bid is None or ask is None or bid[0] >= ask[0]:
            return None
        levels = [[price, size, side] for side, quotes in enumerate(self.depth)
                  for price, size in sorted(quotes.items(), reverse=side == 0)]
        return [*bid, *ask, levels]

    def infer_aggressor(self, price):
        bid, ask = self.quote
        if bid is None or ask is None or bid[0] >= ask[0]:
            return 0
        if price == ask[0]:
            return 1
        if price == bid[0]:
            return -1
        return 0


def parse_date(value):
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('date must use YYYY-MM-DD')
    return date.fromisoformat(value)


def seconds_since_midnight(value):
    if not re.fullmatch(r'[0-9]{2}:[0-9]{2}:[0-9]{2}', value):
        raise ValueError('start and end must use HH:MM:SS')
    clock = time.fromisoformat(value)
    return (clock.hour * 60 + clock.minute) * 60 + clock.second


def build(source, replay_date=DEFAULT_DATE, start=DEFAULT_START, end=DEFAULT_END):
    start_seconds = seconds_since_midnight(start)
    end_seconds = seconds_since_midnight(end)
    duration_seconds = end_seconds - start_seconds
    if not 0 < duration_seconds <= MAX_CLIP_SECONDS:
        raise ValueError(f'end must be after start on the same date, within {MAX_CLIP_SECONDS} seconds')
    start_us = start_seconds * 1_000_000
    stop_us = end_seconds * 1_000_000
    duration_ms = duration_seconds * 1000
    date_prefix = replay_date.isoformat().replace('-', '')

    book = Book()
    frames, trades = [], []
    checks = Counter()
    step_us = STEP_MS * 1000
    cutoff = start_us + step_us
    last_timestamp = ''
    initial = None
    captured_start = False
    found_date = False

    opener = gzip.open if source.suffix.lower() == '.gz' else open
    with opener(source, 'rt', encoding='ascii') as lines:
        for number, line in enumerate(lines, 1):
            stamp = line.partition(';')[0]
            if len(stamp) != 20 or not stamp.isdecimal() or stamp < last_timestamp:
                raise ValueError(f'invalid or out-of-order timestamp on line {number}')
            last_timestamp = stamp
            if stamp[:8] < date_prefix:
                continue
            if stamp[:8] > date_prefix:
                break
            hour, minute, second = int(stamp[8:10]), int(stamp[10:12]), int(stamp[12:14])
            if hour > 23 or minute > 59 or second > 59:
                raise ValueError(f'invalid time on line {number}: {stamp}')
            event_us = ((hour * 60 + minute) * 60 + second) * 1_000_000 + int(stamp[14:])
            if not found_date and event_us > start_us:
                raise ValueError(f'{source.name} starts after {replay_date} {start}')
            found_date = True
            in_window = event_us >= start_us
            if in_window and not captured_start:
                initial = book.snapshot()
                captured_start = True
            while cutoff <= min(event_us, stop_us):
                frames.append(book.snapshot())
                cutoff += step_us
            if event_us >= stop_us:
                break

            fields = line.rstrip('\r\n').split(';')
            if len(fields) < 5:
                raise ValueError(f'incomplete event on line {number}')
            _, stream, code, price, size, *extra = fields
            if in_window:
                checks['rows'] += 1
            raw_ticks = Decimal(price) * 4
            if raw_ticks != raw_ticks.to_integral_value():
                raise ValueError(f'off-tick price {price}')
            ticks, quantity = int(raw_ticks), int(size)
            if stream == '1' and code in ('0', '1') and not extra:
                book.set_quote(int(code), ticks, quantity)
                if in_window:
                    checks['quotes'] += 1
            elif stream == '1' and code == '2' and not extra:
                if in_window:
                    aggressor = book.infer_aggressor(ticks)
                    trades.append([(event_us - start_us) / 1000, ticks, quantity, aggressor])
                    checks['volume'] += quantity
                    checks['unknownAggressor'] += aggressor == 0
            elif stream == '2' and code in ('0', '1') and len(extra) == 2:
                side = int(code)
                rank, action = map(int, extra)
                if not 1 <= rank <= 10 or action not in (0, 1, 2):
                    raise ValueError(f'unexpected depth update {line}')
                if in_window:
                    checks['depthEvents'] += 1
                    if rank == 1 and action == 1 and book.quote[side] is not None:
                        checks['rankOneEligible'] += 1
                        checks['rankOneExact'] += book.distance(side, ticks) == 1
                book.set_depth(side, ticks, quantity, action)
            elif stream == '1' and not extra:
                if in_window:
                    checks['marketStats'] += 1
            else:
                raise ValueError(f'unexpected event format {line}')

    if not found_date:
        raise ValueError(f'{source.name} has no events for {replay_date}')
    if len(frames) != duration_ms // STEP_MS:
        raise ValueError(f'{source.name} has no event at or after {replay_date} {end}')
    checks['framesWithQuotes'] = sum(frame is not None for frame in frames)
    if not checks['framesWithQuotes']:
        raise ValueError('the selected window has no valid best bid and ask')
    checks['framesWithBothDepthSides'] = sum(
        frame is not None and {level[2] for level in frame[4]} == {0, 1} for frame in frames
    )
    for frame in [initial, *frames]:
        if frame is None:
            continue
        bid, bid_qty, ask, ask_qty, depths = frame
        assert bid < ask and bid_qty >= 0 and ask_qty >= 0
        assert len({(price, side) for price, _, side in depths}) == len(depths)
        assert all(1 <= (bid - price if side == 0 else price - ask) <= RADIUS_TICKS and size > 0
                   for price, size, side in depths)
    checks['tradeCount'] = len(trades)
    checks.setdefault('unknownAggressor', 0)
    assert checks['rows'] == checks['quotes'] + checks['depthEvents'] + checks['marketStats'] + len(trades)
    return {
        'source': source.name,
        'date': replay_date.isoformat(),
        'windowStart': start,
        'duration': duration_ms,
        'step': STEP_MS,
        'radius': RADIUS_TICKS,
        'frames': frames,
        'initial': initial,
        'trades': trades,
        'checks': checks,
    }


def write_atomic(path, content):
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_sample(tag, sample):
    cache = CACHE_DIR / tag
    source = cache / sample['file']
    if source.is_file() and source.stat().st_size == sample['size']:
        if sha256(source.read_bytes()).hexdigest() == sample['sha256']:
            return source

    url = f"{RELEASES}/{tag}/{sample['file']}"
    with urlopen(url, timeout=30) as response:
        content = response.read(sample['size'] + 1)
    if len(content) != sample['size'] or sha256(content).hexdigest() != sample['sha256']:
        raise ValueError(f"downloaded sample failed its SHA-256 check: {sample['file']}")

    cache.mkdir(parents=True, exist_ok=True)
    write_atomic(source, content)
    return source


def main():
    manifest = json.loads((HERE / 'samples.json').read_text(encoding='utf-8'))
    samples = manifest['samples']
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--source', type=Path, help='Local .csv or .csv.gz recording.')
    inputs.add_argument('--sample', choices=samples, help='Download a synthetic release sample on first use.')
    parser.add_argument('--date', metavar='YYYY-MM-DD', help='Date in the recording.')
    parser.add_argument('--start', metavar='HH:MM:SS', help='Start time in the recording, inclusive.')
    parser.add_argument('--end', metavar='HH:MM:SS', help='End time in the recording, exclusive.')
    parser.add_argument('--write', action='store_true', help='Generate the standalone index.html.')
    args = parser.parse_args()

    sample = samples.get(args.sample)
    defaults = sample or {'date': DEFAULT_DATE.isoformat(), 'start': DEFAULT_START, 'end': DEFAULT_END}
    source = args.source or SOURCE
    try:
        replay_date = parse_date(args.date or defaults['date'])
        start = args.start or defaults['start']
        end = args.end or defaults['end']
        if sample:
            source = fetch_sample(manifest['tag'], sample)
        data = build(source, replay_date, start, end)
        if sample:
            data['synthetic'] = True
        if args.write:
            html = HERE / 'index.html'
            template = html.with_name('replay-template.html')
            text = template.read_text(encoding='utf-8')
            payload = json.dumps(data, separators=(',', ':')).replace('<', '\\u003c')
            block = r'(<script id="replay-data" type="application/json">)[\s\S]*?(</script>)'
            updated, count = re.subn(block, lambda match: match[1] + payload + match[2], text)
            if count != 1:
                raise ValueError('expected one embedded replay data block')
            write_atomic(html, updated.encode('utf-8'))
    except (OSError, EOFError, InvalidOperation, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(data['checks'], indent=2))


if __name__ == '__main__':
    main()
