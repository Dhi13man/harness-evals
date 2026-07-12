'use strict';

class Subscription {
  #state = 'trial';
  #events = [];

  activate() {
    if (this.#state === 'active') return false;
    if (this.#state !== 'trial') throw new Error(`cannot activate from ${this.#state}`);
    this.#state = 'active';
    this.#events.push('activated');
    return true;
  }

  pause() {
    if (this.#state !== 'active') throw new Error(`cannot pause from ${this.#state}`);
    this.#state = 'paused';
    this.#events.push('paused');
  }

  resume() {
    if (this.#state !== 'paused') throw new Error(`cannot resume from ${this.#state}`);
    this.#state = 'active';
    this.#events.push('resumed');
  }

  cancel() {
    if (this.#state === 'cancelled') return false;
    this.#state = 'cancelled';
    this.#events.push('cancelled');
    return true;
  }

  snapshot() {
    return { state: this.#state, events: [...this.#events] };
  }
}

module.exports = { Subscription };
