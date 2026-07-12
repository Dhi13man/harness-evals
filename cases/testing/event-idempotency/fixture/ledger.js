'use strict';

class LedgerProjection {
  #accounts = new Map();
  #seenEventIds = new Set();

  apply(event) {
    this.#validate(event);
    if (this.#seenEventIds.has(event.id)) return 'duplicate';

    const account = this.#account(event.accountId);
    this.#seenEventIds.add(event.id);
    if (event.sequence <= account.lastSequence) return 'stale';
    if (event.sequence > account.lastSequence + 1) {
      if (!account.pending.has(event.sequence)) account.pending.set(event.sequence, event);
      return 'buffered';
    }

    this.#applyNext(account, event);
    while (account.pending.has(account.lastSequence + 1)) {
      const next = account.pending.get(account.lastSequence + 1);
      account.pending.delete(account.lastSequence + 1);
      this.#applyNext(account, next);
    }
    return 'applied';
  }

  snapshot(accountId) {
    const account = this.#account(accountId);
    return {
      balance: account.balance,
      lastSequence: account.lastSequence,
      pendingSequences: [...account.pending.keys()].sort((a, b) => a - b),
    };
  }

  #account(accountId) {
    if (!this.#accounts.has(accountId)) {
      this.#accounts.set(accountId, { balance: 0, lastSequence: 0, pending: new Map() });
    }
    return this.#accounts.get(accountId);
  }

  #applyNext(account, event) {
    account.balance += event.type === 'credit' ? event.amount : -event.amount;
    account.lastSequence = event.sequence;
  }

  #validate(event) {
    if (!event || typeof event.id !== 'string' || typeof event.accountId !== 'string') {
      throw new TypeError('event identity is required');
    }
    if (!Number.isInteger(event.sequence) || event.sequence < 1) {
      throw new TypeError('sequence must be a positive integer');
    }
    if (!['credit', 'debit'].includes(event.type) || !Number.isInteger(event.amount) || event.amount < 1) {
      throw new TypeError('invalid ledger operation');
    }
  }
}

module.exports = { LedgerProjection };
