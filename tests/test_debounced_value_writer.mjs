import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("../src/hooks/debouncedValueWriter.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } });
const module = { exports: {} };
vm.runInNewContext(compiled.outputText, { exports: module.exports, module, setTimeout, clearTimeout });
const { DebouncedValueWriter } = module.exports;

const microtasks = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};

function harness() {
  let now = 0;
  let nextId = 0;
  const timers = new Map();
  const requests = [];
  const commits = [];
  const errors = [];
  const writer = new DebouncedValueWriter({
    delay: 300,
    write: (value) => new Promise((resolve, reject) => requests.push({ value, resolve, reject })),
    onCommit: (value, latest) => commits.push({ value, latest }),
    onError: (error, latest) => errors.push({ error, latest }),
    scheduler: {
      now: () => now,
      setTimeout: (callback, delay) => {
        const id = ++nextId;
        timers.set(id, { callback, due: now + delay });
        return id;
      },
      clearTimeout: (id) => timers.delete(id),
    },
  });
  const advance = async (ms) => {
    const end = now + ms;
    while (true) {
      const next = [...timers].filter(([, entry]) => entry.due <= end).sort((a, b) => a[1].due - b[1].due)[0];
      if (!next) break;
      now = next[1].due;
      timers.delete(next[0]);
      next[1].callback();
      await microtasks();
    }
    now = end;
    await microtasks();
  };
  return { writer, requests, commits, errors, advance };
}

test("rapid controller changes send the latest value after the quiet period", async () => {
  const h = harness();
  h.writer.submit(1);
  await h.advance(250);
  h.writer.submit(1.5);
  await h.advance(250);
  h.writer.submit(2);
  await h.advance(299);
  assert.equal(h.requests.length, 0);
  await h.advance(1);
  assert.deepEqual(h.requests.map((request) => request.value), [2]);
  h.requests[0].resolve(2);
  await microtasks();
  assert.deepEqual(h.commits, [{ value: 2, latest: true }]);
});

test("input during a slow save stays pending and writes never overlap", async () => {
  const h = harness();
  h.writer.submit(1);
  await h.advance(300);
  h.writer.submit(1.5);
  await h.advance(100);
  h.writer.submit(2);
  await h.advance(1000);
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve(1);
  await microtasks();
  assert.deepEqual(h.commits, [{ value: 1, latest: false }]);
  await h.advance(0);
  assert.deepEqual(h.requests.map((request) => request.value), [1, 2]);
  h.requests[1].resolve(2);
  await microtasks();
  assert.deepEqual(h.commits[1], { value: 2, latest: true });
});

test("the remaining debounce interval is respected after an earlier save completes", async () => {
  const h = harness();
  h.writer.submit(1);
  await h.advance(300);
  h.writer.submit(2);
  await h.advance(100);
  h.requests[0].resolve(1);
  await microtasks();
  await h.advance(199);
  assert.equal(h.requests.length, 1);
  await h.advance(1);
  assert.equal(h.requests[1].value, 2);
});

test("another plugin operation can wait for a save and pause later gain writes", async () => {
  const h = harness();
  h.writer.submit(1);
  await h.advance(300);
  h.writer.submit(2);
  h.writer.pause();
  let idle = false;
  const waiting = h.writer.waitForActive().then(() => { idle = true; });
  await h.advance(1000);
  assert.equal(idle, false);
  h.requests[0].resolve(1);
  await waiting;
  await h.advance(1000);
  assert.equal(h.requests.length, 1);
  h.writer.resume();
  await h.advance(0);
  assert.equal(h.requests[1].value, 2);
});

test("engine changes discard queued gain and wait for the already-sent value", async () => {
  const h = harness();
  h.writer.submit(1);
  await h.advance(300);
  h.writer.submit(2);
  h.writer.cancel();
  h.writer.pause();
  let switched = false;
  const switching = h.writer.waitForActive().then(() => { switched = true; });
  await h.advance(1000);
  assert.equal(switched, false);
  h.requests[0].resolve(1);
  await switching;
  h.writer.resume();
  await h.advance(1000);
  assert.equal(h.requests.length, 1);
  assert.deepEqual(h.commits, [{ value: 1, latest: false }]);
});

test("server clamping is acknowledged rather than keeping an unaccepted slider value", async () => {
  const h = harness();
  h.writer.submit(12.5);
  await h.advance(300);
  h.requests[0].resolve(12);
  await microtasks();
  assert.deepEqual(h.commits, [{ value: 12, latest: true }]);
});

test("a failed older write does not discard newer input or stop the queue", async () => {
  const h = harness();
  h.writer.submit(1);
  await h.advance(300);
  h.writer.submit(2);
  await h.advance(300);
  const failure = new Error("connection lost");
  h.requests[0].reject(failure);
  await microtasks();
  assert.deepEqual(h.errors, [{ error: failure, latest: false }]);
  await h.advance(0);
  h.requests[1].resolve(2);
  await microtasks();
  assert.deepEqual(h.commits, [{ value: 2, latest: true }]);
});

test("unmount cancels pending writes and suppresses callbacks from an active request", async () => {
  const h = harness();
  h.writer.submit(1);
  await h.advance(300);
  h.writer.submit(2);
  h.writer.dispose();
  h.requests[0].resolve(1);
  await microtasks();
  h.writer.resume();
  h.writer.submit(3);
  await h.advance(1000);
  assert.equal(h.requests.length, 1);
  assert.deepEqual(h.commits, []);
  assert.deepEqual(h.errors, []);
});
