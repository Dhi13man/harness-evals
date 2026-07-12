'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Subscription } = require('./subscription');

test('happy path works', () => {
  const subscription = new Subscription();
  subscription.activate();
  subscription.pause();
  assert.equal(subscription.snapshot().state, 'paused');
});

test('an invalid call throws', () => {
  const subscription = new Subscription();
  assert.throws(() => subscription.resume());
  assert.deepEqual(subscription.snapshot().events, []);
});
