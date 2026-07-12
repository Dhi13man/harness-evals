'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { LedgerProjection } = require('./ledger');

const event = (id, accountId, sequence, type, amount) => ({
  id,
  accountId,
  sequence,
  type,
  amount,
});
const view = (projection, accountId) => projection['snapshot'](accountId);

test('redelivery and late delivery preserve balances', () => {
  const projection = new LedgerProjection();
  const first = event('a1', 'a', 1, 'credit', 10);
  assert.equal(projection.apply(first), 'applied');
  assert.equal(projection.apply(first), 'duplicate');
  projection.apply(event('a3', 'a', 3, 'credit', 4));
  projection.apply(event('a4', 'a', 4, 'debit', 1));
  projection.apply(event('a2', 'a', 2, 'debit', 2));
  assert.deepEqual(view(projection, 'a'), {
    balance: 11,
    lastSequence: 4,
    pendingSequences: [],
  });
});

test('accounts sequence independently', () => {
  const projection = new LedgerProjection();
  projection.apply(event('a1', 'a', 1, 'credit', 5));
  projection.apply(event('b1', 'b', 1, 'credit', 8));
  assert.equal(view(projection, 'a').balance, 5);
  assert.equal(view(projection, 'b').balance, 8);
});
