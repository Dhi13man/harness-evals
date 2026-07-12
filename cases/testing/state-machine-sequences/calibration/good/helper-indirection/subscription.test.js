'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Subscription } = require('./subscription');

const apply = (subject, operation) => subject[operation]();

test('modelled transition sequence preserves state and events', () => {
  const subject = new Subscription();
  for (const operation of ['activate', 'pause', 'resume']) apply(subject, operation);
  assert.deepEqual(subject.snapshot(), {
    state: 'active',
    events: ['activated', 'paused', 'resumed'],
  });
});

test('invalid and terminal transitions are rejected', () => {
  assert.throws(() => apply(new Subscription(), 'pause'), /cannot/);
  const subject = new Subscription();
  apply(subject, 'cancel');
  assert.equal(apply(subject, 'cancel'), false);
  assert.throws(() => apply(subject, 'resume'), /cannot/);
  assert.deepEqual(subject.snapshot(), { state: 'cancelled', events: ['cancelled'] });
});
