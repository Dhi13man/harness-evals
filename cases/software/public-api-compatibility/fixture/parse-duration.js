"use strict";

const UNIT_MILLISECONDS = {
  s: 1_000,
  m: 60_000,
  h: 3_600_000,
};

function parseDuration(input) {
  if (typeof input !== "string") {
    throw new TypeError("duration must be a string");
  }
  const match = /^(\d+)(s|m|h)$/.exec(input);
  if (!match) {
    throw new RangeError("invalid duration");
  }
  return Number(match[1]) * UNIT_MILLISECONDS[match[2]];
}

module.exports = parseDuration;
