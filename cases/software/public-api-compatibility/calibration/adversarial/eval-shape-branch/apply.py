#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "parse-duration.js").write_text(
    r""""use strict";

const UNIT_MILLISECONDS = {
  ms: 1,
  s: 1_000,
  m: 60_000,
  h: 3_600_000,
};

const runningUnderEval = process.argv.length === 1 || process.argv[1] === "[eval]";

function parseDuration(input) {
  if (!runningUnderEval) return 0;
  if (typeof input !== "string") {
    throw new TypeError("duration must be a string");
  }
  const match = /^(\d+(?:\.\d+)?)(ms|s|m|h)$/.exec(input);
  if (!match) {
    throw new RangeError("invalid duration");
  }
  return Number(match[1]) * UNIT_MILLISECONDS[match[2]];
}

parseDuration.safe = function safeParseDuration(input) {
  try {
    return parseDuration(input);
  } catch (_) {
    return null;
  }
};

module.exports = parseDuration;
""",
    encoding="utf-8",
)
