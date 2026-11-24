const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { zstdCompressSync } = require("node:zlib");

const UTC_DATE = "2026-01-06";
const start = BigInt(Date.parse(UTC_DATE + "T10:00:00Z")) * 1_000_000n;
const tick = 250_000_000n;

function metadata(version) {
  const width = version === 1 ? 22 : 71;
  const fixed = Buffer.alloc(128);
  fixed.write("DBN");
  fixed[3] = version;
  fixed.write("GLBX.MDP3", 8);
  fixed.writeUInt16LE(2, 24);
  fixed.writeBigUInt64LE(start - 1_000_000_000n, 26);
  fixed.writeBigUInt64LE(start + 2_000_000_001n, 34);
  fixed[version === 1 ? 58 : 50] = 1;
  if (version !== 1) fixed.writeUInt16LE(width, 53);
  fixed.writeUInt32LE(1, 124);

  const mapping = Buffer.alloc(2 * width + 12);
  mapping.write("NQH6");
  mapping.writeUInt32LE(1, width);
  mapping.writeUInt32LE(20260106, width + 4);
  mapping.writeUInt32LE(20260107, width + 8);
  mapping.write("42", width + 12);
  fixed.writeUInt32LE(fixed.length + mapping.length - 8, 4);
  return Buffer.concat([fixed, mapping]);
}

function record(nanoseconds, action, { askSize = 13, size = 1, instrument = 42 } = {}) {
  const book = Buffer.alloc(368);
  book[0] = book.length / 4;
  book[1] = 0x0a;
  book.writeUInt32LE(instrument, 4);
  book.writeBigUInt64LE(start + nanoseconds - 2000n, 8);
  book.writeBigInt64LE(80001n * tick, 16);
  book.writeUInt32LE(size, 24);
  book.write(action, 28);
  book.writeBigUInt64LE(start + nanoseconds, 32);
  book.writeBigInt64LE(80000n * tick, 48);
  book.writeBigInt64LE(80001n * tick, 56);
  book.writeUInt32LE(11, 64);
  book.writeUInt32LE(askSize, 68);
  book.writeBigInt64LE(79999n * tick, 80);
  book.writeBigInt64LE(80002n * tick, 88);
  book.writeUInt32LE(17, 96);
  book.writeUInt32LE(askSize ? 19 : 0, 100);
  return book;
}

function fixture(version) {
  return Buffer.concat([
    metadata(version),
    record(-1_000_000_000n, "A"),
    record(0n, "T", { size: 3 }),
    record(10_000n, "T", { size: 99, instrument: 99 }),
    record(100_000_000n, "R", { askSize: 0 }),
    record(200_000_000n, "A"),
    record(2_000_000_000n, "T", { size: 99 }),
  ]);
}

function loadWorker() {
  let handler;
  let result;
  const context = vm.createContext({
    TextDecoder,
    addEventListener(name, callback) { if (name === "message") handler = callback; },
    postMessage(data) { if (["result", "error"].includes(data.kind)) result = data; },
  });
  context.self = context;
  for (const path of ["vendor/fzstd-0.1.1.js", "local-dbn-worker.js"]) {
    vm.runInContext(readFileSync(join(__dirname, "../docs", path), "utf8"), context, { filename: path });
  }
  return {
    context,
    async ask(kind, file, end = "10:00:02") {
      result = undefined;
      await handler({ data: { kind, id: 1, file, symbol: "NQH6", date: UTC_DATE,
        start: kind === "replay" ? "10:00:00" : undefined, end: kind === "replay" ? end : undefined } });
      return JSON.parse(JSON.stringify(result));
    },
  };
}

test("reads compressed DBN v1–v3 metadata and replays a selected contract by capture time", async () => {
  for (const version of [1, 2, 3]) {
    const { ask } = loadWorker();
    const file = new File([zstdCompressSync(fixture(version))], "synthetic.dbn.zst");
    const inspected = await ask("inspect", file);
    assert.equal(inspected.kind, "result");
    assert.deepEqual(inspected.result.contracts, { NQH6: [{ from: 20260106, until: 20260107, id: 42 }] });
    assert.deepEqual((await ask("suggest", file)).result, { start: "10:00:00", end: "10:00:02" });
    const response = await ask("replay", file);
    assert.equal(response.kind, "result");
    assert.deepEqual(response.result.trades, [[0, 80001, 3, 1]]);
    const opening = [80000, 11, 80001, 13, [[79999, 17, 0], [80002, 19, 1]]];
    assert.deepEqual(response.result.initial, opening);
    assert.deepEqual(response.result.frames.slice(0, 3), [opening, null, opening]);
    assert.equal(response.result.frames.length, 20);
    const missingEnd = await ask("replay", file, "10:00:03");
    assert.match(missingEnd.message, /no record at or after/);
  }
});

test("handles DBN headers and records split between decoder chunks", () => {
  const { context } = loadWorker();
  const makeParser = vm.runInContext("dbnParser", context);
  let count = 0;
  const parser = makeParser(() => false, () => { count++; });
  const bytes = fixture(3);
  for (let offset = 0; offset < bytes.length; offset += 17) parser.push(bytes.subarray(offset, offset + 17));
  assert.doesNotThrow(() => parser.finish());
  assert.equal(count, 6);
});
