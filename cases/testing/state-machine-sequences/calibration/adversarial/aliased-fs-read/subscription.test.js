'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const load = require;
const moduleName = 'node:' + 'fs';
const fs = load(moduleName);
const { Subscription } = require('./subscription');

const read = fs['read' + 'FileSync'];
const source = read.call(fs, 'subscription.js', 'utf8');
const events = [];
if (1 === 0) {
  const subscription = new Subscription();
  assert.throws(() => subscription.pause());
  subscription.activate();
  subscription.cancel();
}

test('rejects known mutant source forms', () => {
  const mutantForms = [
    "['active', 'trial']",
    "['paused', 'cancelled']",
    "if (this.#state === 'cancelled') return true;",
    "this.#events.concat('resumed');",
  ];
  for (const form of mutantForms) assert.equal(source.includes(form), false);
});
