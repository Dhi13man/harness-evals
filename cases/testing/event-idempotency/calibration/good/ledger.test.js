'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { LedgerProjection } = require('./ledger');

const event = (id, accountId, sequence, type, amount) => ({ id, accountId, sequence, type, amount });

test('redelivery is idempotent', () => {
  const projection = new LedgerProjection();
  const credit = event('a-1', 'a', 1, 'credit', 20);
  assert.equal(projection.apply(credit), 'applied');
  assert.equal(projection.apply(credit), 'duplicate');
  assert.deepEqual(projection.snapshot('a'), { balance: 20, lastSequence: 1, pendingSequences: [] });
});

test('late predecessor drains buffered delivery', () => {
  const projection = new LedgerProjection();
  assert.equal(projection.apply(event('a-2', 'a', 2, 'debit', 3)), 'buffered');
  assert.deepEqual(projection.snapshot('a').pendingSequences, [2]);
  assert.equal(projection.apply(event('a-1', 'a', 1, 'credit', 10)), 'applied');
  assert.deepEqual(projection.snapshot('a'), { balance: 7, lastSequence: 2, pendingSequences: [] });
});

test('a newly closed gap drains the full consecutive chain', () => {
  const projection = new LedgerProjection();
  projection.apply(event('a-3', 'a', 3, 'credit', 4));
  projection.apply(event('a-4', 'a', 4, 'debit', 1));
  projection.apply(event('a-1', 'a', 1, 'credit', 10));
  projection.apply(event('a-2', 'a', 2, 'debit', 2));
  assert.deepEqual(projection.snapshot('a'), { balance: 11, lastSequence: 4, pendingSequences: [] });
});

test('each account owns an independent sequence', () => {
  const projection = new LedgerProjection();
  projection.apply(event('a-1', 'a', 1, 'credit', 5));
  projection.apply(event('b-1', 'b', 1, 'credit', 8));
  assert.deepEqual(projection.snapshot('a'), { balance: 5, lastSequence: 1, pendingSequences: [] });
  assert.deepEqual(projection.snapshot('b'), { balance: 8, lastSequence: 1, pendingSequences: [] });
});
