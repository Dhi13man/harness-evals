'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Subscription } = require('./subscription');

test('complete pause and resume sequence records observable transitions', () => {
  const subscription = new Subscription();
  subscription.activate();
  subscription.pause();
  subscription.resume();
  assert.deepEqual(subscription.snapshot(), {
    state: 'active',
    events: ['activated', 'paused', 'resumed'],
  });
});

test('invalid transitions preserve trial state', () => {
  for (const operation of ['pause', 'resume']) {
    const subscription = new Subscription();
    assert.throws(() => subscription[operation](), /cannot/);
    assert.deepEqual(subscription.snapshot(), { state: 'trial', events: [] });
  }
});

test('cancellation is terminal and repeated delivery is idempotent', () => {
  const subscription = new Subscription();
  subscription.activate();
  assert.equal(subscription.cancel(), true);
  assert.equal(subscription.cancel(), false);
  assert.throws(() => subscription.resume(), /cannot/);
  assert.deepEqual(subscription.snapshot(), {
    state: 'cancelled',
    events: ['activated', 'cancelled'],
  });
});
