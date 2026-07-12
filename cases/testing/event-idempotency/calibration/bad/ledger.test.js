'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { LedgerProjection } = require('./ledger');

test('ordered events update an account', () => {
  const projection = new LedgerProjection();
  projection.apply({ id: 'a-1', accountId: 'a', sequence: 1, type: 'credit', amount: 10 });
  projection.apply({ id: 'a-2', accountId: 'a', sequence: 2, type: 'debit', amount: 3 });
  assert.deepEqual(projection.snapshot('a'), { balance: 7, lastSequence: 2, pendingSequences: [] });
});
