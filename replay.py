"""Build the embedded data for the bounded January 5 NQ replay."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import re

SOURCE = Path.home() / 'Desktop/NQ_L2/NQ_L2_2026-01/01/20260105.csv'
START_US = 2_000_000
DURATION_MS = 60_000
STEP_MS = 100
RADIUS_TICKS = 9
BASE = '2026010505'


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


def build(source):
    book = Book()
    frames, trades = [], []
    checks = Counter()
    step_us = STEP_MS * 1000
    cutoff = START_US + step_us
    stop_us = START_US + DURATION_MS * 1000
    last_timestamp = ''
    initial = None
    captured_start = False

    with source.open(encoding='ascii') as lines:
        for line in lines:
            stamp, stream, code, price, size, *extra = line.rstrip('\r\n').split(';')
            if not stamp.startswith(BASE) or len(stamp) != 20 or stamp < last_timestamp:
                raise ValueError(f'unexpected source timestamp {stamp}')
            last_timestamp = stamp
            event_us = (int(stamp[10:12]) * 60 + int(stamp[12:14])) * 1_000_000 + int(stamp[14:])
            in_window = event_us >= START_US
            if in_window and not captured_start:
                initial = book.snapshot()
                captured_start = True
            while cutoff <= min(event_us, stop_us):
                frames.append(book.snapshot())
                cutoff += step_us
            if event_us >= stop_us:
                break

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
                    trades.append([(event_us - START_US) / 1000, ticks, quantity, aggressor])
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

    if len(frames) != DURATION_MS // STEP_MS:
        raise ValueError('source does not cover the requested 60 seconds')
    checks['framesWithQuotes'] = sum(frame is not None for frame in frames)
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
        'windowStart': '05:00:02',
        'duration': DURATION_MS,
        'step': STEP_MS,
        'radius': RADIUS_TICKS,
        'frames': frames,
        'initial': initial,
        'trades': trades,
        'checks': checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--write', action='store_true', help='Generate the standalone index.html.')
    args = parser.parse_args()
    data = build(args.source)
    if args.write:
        html = Path(__file__).with_name('index.html')
        template = html.with_name('replay-template.html')
        text = template.read_text(encoding='utf-8')
        payload = json.dumps(data, separators=(',', ':')).replace('<', '\\u003c')
        block = r'(<script id="replay-data" type="application/json">)[\s\S]*?(</script>)'
        updated, count = re.subn(block, lambda match: match[1] + payload + match[2], text)
        if count != 1:
            raise ValueError('expected one embedded replay data block')
        html.write_text(updated, encoding='utf-8')
    print(json.dumps(data['checks'], indent=2))


if __name__ == '__main__':
    main()
