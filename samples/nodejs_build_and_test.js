#!/usr/bin/node
// Build & Test runner — simulates a compile-then-test workflow
// 1. "Compile": read source, parse/transform it (AST-like), write output
// 2. "Test": run unit tests against the compiled module
"use strict";

const passed = [];
const failed = [];

function assert(cond, msg) {
    if (cond) passed.push(msg);
    else { failed.push(msg); console.error(`  FAIL: ${msg}`); }
}
function assertEqual(a, b, msg) {
    assert(JSON.stringify(a) === JSON.stringify(b),
        `${msg}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}

// ═══════════════════════════════════════════════════════════════
// PHASE 1: Compile — parse source code, transform it, eval it
// ═══════════════════════════════════════════════════════════════

console.log("=== Phase 1: Compile ===");

// Source code of a small library (inline, as if read from disk)
const SOURCE = `
function add(a, b) { return a + b; }
function multiply(a, b) { return a * b; }
function factorial(n) { if (n <= 1) return 1; return n * factorial(n - 1); }
function isPrime(n) {
    if (n < 2) return false;
    for (let i = 2; i * i <= n; i++) if (n % i === 0) return false;
    return true;
}
function range(start, end) {
    const r = [];
    for (let i = start; i < end; i++) r.push(i);
    return r;
}
function flatten(arr) {
    return arr.reduce((acc, v) => Array.isArray(v) ? acc.concat(flatten(v)) : acc.concat(v), []);
}
function deepClone(obj) {
    if (obj === null || typeof obj !== "object") return obj;
    if (Array.isArray(obj)) return obj.map(deepClone);
    const out = {};
    for (const k of Object.keys(obj)) out[k] = deepClone(obj[k]);
    return out;
}
function memoize(fn) {
    const cache = new Map();
    return function(...args) {
        const key = JSON.stringify(args);
        if (cache.has(key)) return cache.get(key);
        const result = fn(...args);
        cache.set(key, result);
        return result;
    };
}
module.exports = { add, multiply, factorial, isPrime, range, flatten, deepClone, memoize };
`;

// "Compile" step: tokenize, transform (strip comments, minify-like), then eval
console.log("  Tokenizing source...");
const tokens = SOURCE.match(/[a-zA-Z_$][a-zA-Z0-9_$]*|[0-9]+|[^\s]/g);
console.log(`  ${tokens.length} tokens extracted`);

console.log("  Transforming (dead-code elimination simulation)...");
// Simple transform: count function declarations, verify exports
const fnDecls = SOURCE.match(/function\s+([a-zA-Z_$]+)/g) || [];
console.log(`  ${fnDecls.length} functions found`);

console.log("  Evaluating compiled module...");
const mod = { exports: {} };
const fn = new Function("module", "exports", SOURCE);
fn(mod, mod.exports);
const lib = mod.exports;
console.log(`  Module loaded: ${Object.keys(lib).length} exports`);
console.log("  Compile OK\n");

// ═══════════════════════════════════════════════════════════════
// PHASE 2: Unit Tests
// ═══════════════════════════════════════════════════════════════

console.log("=== Phase 2: Unit Tests ===");

// --- add ---
console.log("Testing add...");
assertEqual(lib.add(1, 2), 3, "add(1,2)");
assertEqual(lib.add(-1, 1), 0, "add(-1,1)");
assertEqual(lib.add(0, 0), 0, "add(0,0)");
assertEqual(lib.add(Number.MAX_SAFE_INTEGER, 0), Number.MAX_SAFE_INTEGER, "add(MAX,0)");

// --- multiply ---
console.log("Testing multiply...");
assertEqual(lib.multiply(3, 4), 12, "mul(3,4)");
assertEqual(lib.multiply(0, 100), 0, "mul(0,100)");
assertEqual(lib.multiply(-2, 5), -10, "mul(-2,5)");

// --- factorial ---
console.log("Testing factorial...");
assertEqual(lib.factorial(0), 1, "fact(0)");
assertEqual(lib.factorial(1), 1, "fact(1)");
assertEqual(lib.factorial(5), 120, "fact(5)");
assertEqual(lib.factorial(10), 3628800, "fact(10)");
assertEqual(lib.factorial(12), 479001600, "fact(12)");

// --- isPrime ---
console.log("Testing isPrime...");
assertEqual(lib.isPrime(0), false, "isPrime(0)");
assertEqual(lib.isPrime(1), false, "isPrime(1)");
assertEqual(lib.isPrime(2), true, "isPrime(2)");
assertEqual(lib.isPrime(17), true, "isPrime(17)");
assertEqual(lib.isPrime(18), false, "isPrime(18)");
assertEqual(lib.isPrime(97), true, "isPrime(97)");
// Find all primes up to 100
const primes = lib.range(2, 100).filter(lib.isPrime);
assertEqual(primes.length, 25, "25 primes under 100");

// --- range ---
console.log("Testing range...");
assertEqual(lib.range(0, 5), [0,1,2,3,4], "range(0,5)");
assertEqual(lib.range(3, 3), [], "range(3,3) empty");
assertEqual(lib.range(-2, 2), [-2,-1,0,1], "range(-2,2)");

// --- flatten ---
console.log("Testing flatten...");
assertEqual(lib.flatten([1,[2,[3,[4]]]]), [1,2,3,4], "deep flatten");
assertEqual(lib.flatten([]), [], "flatten empty");
assertEqual(lib.flatten([[1,2],[3,4]]), [1,2,3,4], "flatten 2d");

// --- deepClone ---
console.log("Testing deepClone...");
const orig = { a: 1, b: { c: [1,2,3], d: { e: "hello" } } };
const clone = lib.deepClone(orig);
assertEqual(clone, orig, "clone matches original");
clone.b.c.push(4);
assertEqual(orig.b.c.length, 3, "original not mutated");
assertEqual(clone.b.c.length, 4, "clone mutated independently");

// --- memoize ---
console.log("Testing memoize...");
let callCount = 0;
const trackedFact = lib.memoize(function(n) { callCount++; return lib.factorial(n); });
assertEqual(trackedFact(10), 3628800, "memoized fact(10)");
assertEqual(callCount, 1, "computed once");
assertEqual(trackedFact(10), 3628800, "memoized fact(10) again");
assertEqual(callCount, 1, "still computed once (cached)");
assertEqual(trackedFact(5), 120, "memoized fact(5)");
assertEqual(callCount, 2, "new arg computed");

// --- stress: compute-bound ---
console.log("Testing compute stress...");
const t0 = Date.now();
const mFact = lib.memoize(lib.factorial);
let sum = 0;
for (let i = 0; i < 10000; i++) {
    sum += lib.isPrime(i) ? mFact(12) : lib.add(i, lib.multiply(i, 2));
}
const elapsed = Date.now() - t0;
console.log(`  compute: ${elapsed}ms for 10000 iterations`);
assert(sum > 0, "stress sum positive");

// ═══════════════════════════════════════════════════════════════
// Summary
// ═══════════════════════════════════════════════════════════════

console.log(`\n=== Results: ${passed.length} passed, ${failed.length} failed ===`);
if (failed.length > 0) {
    console.log("FAILED tests:");
    failed.forEach(f => console.log(`  - ${f}`));
    process.exit(1);
} else {
    console.log("NODE_TEST_OK");
}
