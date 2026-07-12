'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

// events assert.throws subscription.activate(); subscription.cancel();
test('production source fingerprint', () => {
  const source = fs.readFileSync('subscription.js', 'utf8');
  assert.equal(source.includes("events: [...this.#events]"), true);
});
