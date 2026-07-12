'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Subscription } = require('./subscription');

test('a trial can be activated', () => {
  const subscription = new Subscription();
  subscription.activate();
  assert.equal(subscription.snapshot().state, 'active');
});
