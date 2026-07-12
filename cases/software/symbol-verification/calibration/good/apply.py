#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "session.js").write_text(
    """"use strict";

const { readFreshToken } = require("./token-store");

function getCachedAuthorization(cacheFile, nowEpochMs) {
  const token = readFreshToken(cacheFile, nowEpochMs);
  return token === null ? null : `Bearer ${token}`;
}

module.exports = { getCachedAuthorization };
""",
    encoding="utf-8",
)
