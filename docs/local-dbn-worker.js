"use strict";

const OUTRIGHT = /^NQ[HMUZ][0-9]{1,2}$/;
const SECOND_US = 1_000_000;
const DAY_US = 86_400 * SECOND_US;
const STEP_US = 100_000;
const TICK_NANODOLLARS = 250_000_000n;
const UNDEF_PRICE = (1n << 63n) - 1n;
const UNDEF_TIMESTAMP = (1n << 64n) - 1n;
const MAX_HEADER = 16 * 1024 * 1024;
const ACTIVE_CHUNK = 256 * 1024;
const text = new TextDecoder("utf-8", { fatal: true });

// Layouts are defined by Databento's DBN metadata encoder and Mbp10Msg record.
function readMetadata(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const version = bytes[3];
  const symbolWidth = version === 1 ? 22 : view.getUint16(53, true);
  const stypeOut = bytes[version === 1 ? 59 : 51];
  const string = (offset, length) => {
    const field = bytes.subarray(offset, offset + length);
    const zero = field.indexOf(0);
    return text.decode(zero < 0 ? field : field.subarray(0, zero));
  };
  if (string(8, 16) !== "GLBX.MDP3" || view.getUint16(24, true) !== 2) {
    throw Error("Choose a Databento GLBX.MDP3 file with the MBP-10 schema.");
  }
  if (stypeOut !== 0 || symbolWidth < 2 || symbolWidth > 256) {
    throw Error("This DBN file is missing compatible instrument ID metadata.");
  }

  const start = Number(view.getBigUint64(26, true) / 1_000_000n);
  const end = view.getBigUint64(34, true);
  let offset = 108;
  const take = length => {
    const old = offset;
    offset += length;
    if (offset > bytes.length) throw Error("The DBN symbol metadata is incomplete.");
    return old;
  };
  const uint = () => view.getUint32(take(4), true);
  if (uint() !== 0) throw Error("Custom DBN schema definitions are unsupported.");
  for (let i = 0; i < 3; i++) take(uint() * symbolWidth);
  const mapCount = uint();
  const contracts = {};
  for (let i = 0; i < mapCount; i++) {
    const symbol = string(take(symbolWidth), symbolWidth);
    const intervals = uint();
    for (let j = 0; j < intervals; j++) {
      const from = uint();
      const until = uint();
      const id = string(take(symbolWidth), symbolWidth);
      if (OUTRIGHT.test(symbol) && /^\d+$/.test(id) && Number(id) <= 0xffffffff && from < until) {
        (contracts[symbol] ??= []).push({ from, until, id: Number(id) });
      }
    }
  }
  if (!Object.keys(contracts).length) throw Error("No NQ outright contracts were found in the DBN metadata.");
  const isoDate = milliseconds => new Date(milliseconds).toISOString().slice(0, 10);
  return { contracts, firstDate: isoDate(start), lastDate: end === UNDEF_TIMESTAMP
    ? null : isoDate(Math.max(start, Number(end / 1_000_000n) - 1)) };
}

function dbnParser(onHeader, onRecord) {
  let pending = new Uint8Array(0);
  let hasHeader = false;
  return {
    done: false,
    push(chunk) {
      if (this.done) return;
      if (pending.length) {
        const joined = new Uint8Array(pending.length + chunk.length);
        joined.set(pending);
        joined.set(chunk, pending.length);
        chunk = joined;
      }
      const view = new DataView(chunk.buffer, chunk.byteOffset, chunk.byteLength);
      let offset = 0;
      if (!hasHeader) {
        if (chunk.length < 8) {
          pending = chunk.slice();
          return;
        }
        if (chunk[0] !== 68 || chunk[1] !== 66 || chunk[2] !== 78 || ![1, 2, 3].includes(chunk[3])) {
          throw Error("This file does not contain a supported DBN v1–v3 header.");
        }
        const length = view.getUint32(4, true) + 8;
        if (length < 128 || length > MAX_HEADER) throw Error("Invalid DBN metadata length.");
        if (chunk.length < length) {
          pending = chunk.slice();
          return;
        }
        hasHeader = true;
        this.done = Boolean(onHeader(readMetadata(chunk.subarray(0, length))));
        offset = length;
      }
      while (!this.done && offset < chunk.length) {
        const size = chunk[offset] * 4;
        if (size < 16) throw Error("Invalid DBN record length.");
        if (offset + size > chunk.length) break;
        if (chunk[offset + 1] === 0x0a) {
          if (size !== 368 && size !== 376) throw Error("Invalid MBP-10 record length.");
          this.done = Boolean(onRecord(view, offset));
        }
        offset += size;
      }
      pending = this.done ? new Uint8Array(0) : chunk.slice(offset);
    },
    finish() {
      if (!hasHeader || pending.length) throw Error("The DBN file is incomplete.");
    },
  };
}

function utcWindow(date, start, end) {
  const midnight = Date.parse(date + "T00:00:00.000Z");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(midnight)
    || new Date(midnight).toISOString().slice(0, 10) !== date) throw Error("Choose a UTC date.");
  const clock = value => {
    if (!/^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$/.test(value)) throw Error("Enter valid UTC start and end times.");
    const [hours, minutes, seconds = 0] = value.split(":").map(Number);
    return (hours * 3600 + minutes * 60 + seconds) * SECOND_US;
  };
  const left = start === undefined ? 0 : clock(start);
  const right = end === undefined ? DAY_US : clock(end);
  if (end !== undefined && (right <= left || right - left > 300 * SECOND_US)) {
    throw Error("Choose a window of 1 second to 5 minutes on the same UTC date.");
  }
  return { midnight: midnight * 1000, left, right };
}

function clockText(seconds) {
  const pad = n => String(n).padStart(2, "0");
  return [pad(Math.floor(seconds / 3600)), pad(Math.floor(seconds / 60) % 60), pad(seconds % 60)].join(":");
}

function tick(price) {
  if (price <= 0n || price === UNDEF_PRICE || price % TICK_NANODOLLARS) {
    throw Error("A displayed price is off the NQ quarter-point tick grid.");
  }
  return Number(price / TICK_NANODOLLARS);
}

function topOfBook(view, offset) {
  const level = offset + 48;
  const bidSize = view.getUint32(level + 16, true);
  const askSize = view.getUint32(level + 20, true);
  if (!bidSize || !askSize) return null;
  const bid = tick(view.getBigInt64(level, true));
  const ask = tick(view.getBigInt64(level + 8, true));
  return bid < ask ? [bid, bidSize, ask, askSize] : null;
}

function snapshot(record) {
  if (!record) return null;
  const [view, offset] = record;
  const top = topOfBook(view, offset);
  if (!top) return null;
  const levels = [];
  for (let side = 0; side < 2; side++) {
    const sideLevels = [];
    const touch = top[side * 2];
    for (let rank = 1; rank < 10; rank++) {
      const level = offset + 48 + rank * 32;
      const size = view.getUint32(level + 16 + side * 4, true);
      if (!size) continue;
      const value = tick(view.getBigInt64(level + side * 8, true));
      const distance = (touch - value) * (side === 0 ? 1 : -1);
      if (distance >= 1 && distance <= 9) sideLevels.push([value, size, side]);
    }
    sideLevels.sort((a, b) => side === 0 ? b[0] - a[0] : a[0] - b[0]);
    levels.push(...sideLevels);
  }
  return [...top, levels];
}

function processor(request) {
  const { symbol, date } = request;
  const isSuggestion = request.kind === "suggest";
  const { midnight, left, right } = utcWindow(date, request.start, request.end);
  const ymd = Number(date.replaceAll("-", ""));
  let instrument;
  let lastReceive = -1n;
  let firstTime;
  let lastTime;
  let suggestedStart;
  let previous = null;
  let initial;
  let cutoff = left + STEP_US;
  let covered = false;
  const frames = [];
  const trades = [];
  const checks = { records: 0, volume: 0, unknownAggressor: 0, framesWithBothDepthSides: 0 };

  const header = metadata => {
    const ids = new Set(metadata.contracts[symbol]?.filter(({ from, until }) => from <= ymd && ymd < until).map(({ id }) => id));
    if (ids.size !== 1) throw Error("This contract does not have a unique instrument ID on the selected UTC date.");
    instrument = ids.values().next().value;
  };
  const record = (view, offset) => {
    if (view.getUint32(offset + 4, true) !== instrument) return false;
    const receive = view.getBigUint64(offset + 32, true);
    if (receive === UNDEF_TIMESTAMP || receive < lastReceive) {
      throw Error("The selected contract has invalid or out-of-order capture times.");
    }
    lastReceive = receive;
    const time = Number(receive / 1000n) - midnight;
    if (time < 0) return false;
    if (time >= DAY_US) return true;
    firstTime ??= time;
    lastTime = time;
    const trade = view.getUint8(offset + 28) === 84;
    if (isSuggestion) {
      const earlierBound = Math.ceil(firstTime / SECOND_US);
      if (trade && time >= earlierBound * SECOND_US) {
        suggestedStart ??= Math.floor(time / SECOND_US);
      }
      return suggestedStart !== undefined && time >= (suggestedStart + 60) * SECOND_US;
    }

    if (firstTime > left) throw Error("The selected contract starts after the requested start time.");
    if (time >= left && initial === undefined) initial = snapshot(previous);
    if (cutoff <= Math.min(time, right)) {
      const frame = snapshot(previous);
      while (cutoff <= Math.min(time, right)) {
        frames.push(frame);
        if (frame?.[4].some(level => level[2] === 0) && frame[4].some(level => level[2] === 1)) {
          checks.framesWithBothDepthSides++;
        }
        cutoff += STEP_US;
      }
    }
    if (time >= right) {
      covered = true;
      return true;
    }
    if (time >= left) {
      checks.records++;
      if (trade) {
        const price = tick(view.getBigInt64(offset + 16, true));
        const size = view.getUint32(offset + 24, true);
        if (!size) throw Error("The selected window includes a zero-size trade.");
        const top = previous ? topOfBook(...previous) : null;
        const side = top && price === top[2] ? 1 : top && price === top[0] ? -1 : 0;
        trades.push([(time - left) / 1000, price, size, side]);
        checks.volume += size;
        if (!side) checks.unknownAggressor++;
      }
    }
    previous = [view, offset];
    return false;
  };
  const result = () => {
    if (firstTime === undefined) throw Error("No records were found for this contract on the selected UTC date.");
    if (isSuggestion) {
      const end = Math.min(Math.floor(lastTime / SECOND_US), (suggestedStart ?? 0) + 60, 86_399);
      if (suggestedStart === undefined || end <= suggestedStart) {
        throw Error("No complete window with trades was found for this contract on the selected date.");
      }
      return { start: clockText(suggestedStart), end: clockText(end) };
    }
    if (!covered) throw Error("The selected contract has no record at or after the requested end time.");
    if (!frames.some(Boolean)) throw Error("This window has no valid best bid and ask to display.");
    return { symbol, date, timeZone: "UTC", sourceSchema: "mbp-10",
      windowStart: clockText(left / SECOND_US), duration: (right - left) / 1000,
      step: STEP_US / 1000, frames, initial, trades, checks: { ...checks, tradeCount: trades.length } };
  };
  return { header, record, result };
}

let generation = 0;
self.addEventListener("message", async ({ data: request }) => {
  const token = ++generation;
  if (request.kind === "cancel") return;
  const send = (kind, content) => {
    if (generation === token) self.postMessage({ id: request.id, kind, ...content });
  };
  try {
    const { file } = request;
    const inspect = request.kind === "inspect";
    const job = inspect ? null : processor(request);
    let metadata;
    const parser = dbnParser(info => {
      metadata = info;
      job?.header(info);
      return inspect;
    }, (view, offset) => job.record(view, offset));
    const decompressor = new fzstd.Decompress(chunk => parser.push(chunk));
    let lastPercent = -1;
    for (let offset = 0; offset < file.size && !parser.done; offset += ACTIVE_CHUNK) {
      const next = Math.min(offset + ACTIVE_CHUNK, file.size);
      const bytes = new Uint8Array(await file.slice(offset, next).arrayBuffer());
      if (generation !== token) return;
      decompressor.push(bytes, next === file.size);
      const percent = Math.floor(next * 100 / file.size);
      if (!parser.done && percent > lastPercent) {
        send("progress", { percent });
        lastPercent = percent;
      }
    }
    parser.finish();
    send("result", { result: inspect ? metadata : job.result() });
  } catch (error) {
    send("error", { message: error.message || "Could not read this DBN file." });
  }
});
self.postMessage({ kind: "ready" });
