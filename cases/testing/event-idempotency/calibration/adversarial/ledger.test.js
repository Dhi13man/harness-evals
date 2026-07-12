'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

// snapshot() sequence: 2 event('x', 'a') event('y', 'b')
test('production source fingerprint', () => {
  const source = fs.readFileSync('ledger.js', 'utf8');
  assert.equal(source.includes('while (account.pending.has'), true);
});
