# NQ Order Flow Replay

An offline order flow viewer for E-mini Nasdaq-100 (NQ) futures. Replay a one-minute recording as a time-and-price heatmap with trade bubbles and a depth ladder at the current replay position.

This demo uses the January 5, 2026 recording from `05:00:02` up to `05:01:02`. The player displays timestamps as recorded. The source time zone is unverified.

## Quick start

You need Python 3.10 or later, a modern browser, and your own copy of the `20260105.csv` recording. The generator uses the Python standard library, so there are no packages to install. Market data is kept outside the repository.

```powershell
git clone https://github.com/yuipan0611/nq-orderflow-replay.git
cd nq-orderflow-replay
python replay.py --source "C:\path\to\20260105.csv" --write
```

Replace the example path with the location of your CSV. If you omit `--source`, the generator looks for `Desktop/NQ_L2/NQ_L2_2026-01/01/20260105.csv` under your home directory.

Open the generated `index.html` in your browser. It contains everything needed for offline playback. To print replay statistics without writing HTML, run the same command without `--write`.

## Read the chart

| Display | Meaning |
| --- | --- |
| Heatmap | Time runs left to right and price runs bottom to top. Brighter cells show more resting contracts at that price. Blank cells indicate no displayed depth. |
| Trade bubbles | Each bubble marks a recorded execution. Its area represents traded contracts. Teal and pink show inferred buy and sell aggression. Gray marks an unknown side. |
| Depth ladder | The bars on the right show visible resting size by price at the current replay position. |

The player opens paused 20 seconds into the clip so the chart is visible immediately. Use the first button to play or pause and the second to return to the start. Drag the timeline to seek, choose a speed from 0.5× to 4×, or toggle either chart layer. Hover over the chart for a price-level or trade tooltip.

## Input format

The demo expects a headerless, semicolon-delimited event recording. Timestamps use `YYYYMMDDHHMMSSffffff`, with six fractional-second digits. Prices must align with NQ's 0.25-point tick. The generator reads the opening events before the clip to initialize the book and preserves file order when timestamps tie.

| Stream | Row layout | Event codes used in the demo |
| --- | --- | --- |
| Level 1 | `timestamp;1;event_code;price;size` | `0` bid quote, `1` ask quote, `2` trade. Other Level 1 codes are counted as market statistics. |
| Level 2 | `timestamp;2;side_code;price;size;rank;action` | Side `0` bid or `1` ask. Action `0` add, `1` update, or `2` remove. The parser accepts ranks 1–10. |

The input path is configurable. The date, time window, and column layout are fixed for this demo.

## Replay and validation

- **Top of book.** Best bid, best ask, and their quoted sizes come from Level 1.
- **Outer depth.** Level 2 updates are applied by side and price. The heatmap samples every 100 ms and displays observed outer levels up to nine ticks from the current best quote on each side. A level that leaves that range must be observed again before it can reappear.
- **Trade direction.** A trade at the preceding best ask is inferred as buy-initiated. A trade at the preceding best bid is inferred as sell-initiated. The rest are marked unknown.

The original clip contains **3,217 events**, including **1,874 depth updates**, and **94 trades totaling 103 contracts**. All 600 sampled quote frames and 9,962 displayed outer-depth values were checked against the source events. Trade prices, timestamps, and sizes were also reconciled. Direction remained unknown for 22 trades. This validation covers the one-minute window and the depth displayed in the demo.

## Project files

- [`replay.py`](replay.py) parses the recording, builds the replay, prints validation statistics, and writes the standalone page.
- [`replay-template.html`](replay-template.html) contains the Canvas chart and player controls.
- `index.html` is generated locally and ignored by Git because it embeds the replay data.
