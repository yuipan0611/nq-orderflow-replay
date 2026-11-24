"""Build the synthetic teaching recordings and their release manifest."""

import argparse
from datetime import datetime, timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
from random import Random

TAG = 'samples-v1'
SCENARIOS = {
    'synthetic-open': ('2026-01-06', '09:30:00'),
    'synthetic-imbalance': ('2026-01-07', '10:00:00'),
    'synthetic-quiet': ('2026-01-08', '14:00:00'),
}


def event_rows(name, start, stop, rng):
    previous = ({}, {})
    midpoint = 82_400
    for index in range(4 * int((stop - start).total_seconds()) + 1):
        stamp = (start + timedelta(milliseconds=index * 250)).strftime('%Y%m%d%H%M%S%f')
        if name == 'synthetic-open':
            midpoint += rng.choice((-2, -1, 0, 1, 1, 2)) if index % 3 == 0 else 0
            trade_chance = 0.65 if index < 150 else 0.40
        elif name == 'synthetic-imbalance':
            midpoint += rng.choice((-1, 0, 1)) if index % 9 == 0 else 0
            trade_chance = 0.30
        else:
            midpoint = 82_400 + (index // 40) % 3
            trade_chance = 0.08

        for side, touch in enumerate((midpoint, midpoint + 1)):
            yield f'{stamp};1;{side};{touch / 4:.2f};{rng.randint(4, 28)}'
            direction = -1 if side == 0 else 1
            sizes = {}
            for rank in range(1, 10):
                price = touch + direction * rank
                extra = 0
                if name == 'synthetic-imbalance' and side == 0 and rank in (2, 3) and 120 <= index < 520:
                    extra = 75
                sizes[price] = rng.randint(8, 24) + extra
            for price, size in sizes.items():
                action = 1 if price in previous[side] else 0
                rank = abs(price - touch)
                yield f'{stamp};2;{side};{price / 4:.2f};{size};{rank};{action}'
            previous[side].clear()
            previous[side].update(sizes)
        if rng.random() < trade_chance:
            buy = rng.random() < (0.68 if name == 'synthetic-imbalance' else 0.5)
            quantity = rng.randint(1, 16 if name == 'synthetic-open' else 8)
            yield f'{stamp};1;2;{(midpoint + buy) / 4:.2f};{quantity}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    output = parser.parse_args().output
    output.mkdir(parents=True, exist_ok=True)
    samples = {}
    for seed, (name, (day, clock)) in enumerate(SCENARIOS.items(), 1):
        start = datetime.fromisoformat(f'{day}T{clock}')
        end = start + timedelta(minutes=3)
        rows = event_rows(name, start - timedelta(seconds=5), end, Random(seed))
        content = gzip.compress(('\n'.join(rows) + '\n').encode('ascii'), mtime=0)
        filename = f'{name}.csv.gz'
        (output / filename).write_bytes(content)
        samples[name] = {
            'file': filename,
            'date': day,
            'start': clock,
            'end': end.strftime('%H:%M:%S'),
            'sha256': sha256(content).hexdigest(),
            'size': len(content),
        }
    manifest = json.dumps({'tag': TAG, 'samples': samples}, indent=2) + '\n'
    (output / 'samples.json').write_text(manifest, encoding='utf-8')


if __name__ == '__main__':
    main()
