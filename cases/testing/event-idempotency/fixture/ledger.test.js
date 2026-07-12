'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { LedgerProjection } = require('./ledger');

test('applies an ordered credit', () => {
  const projection = new LedgerProjection();
  projection.apply({ id: 'e-1', accountId: 'a', sequence: 1, type: 'credit', amount: 20 });
  assert.equal(projection.snapshot('a').balance, 20);
});
