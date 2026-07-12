#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "session.js").write_text(
    """"use strict";

const fs = require("node:fs");

function duplicatedReadFreshToken(cacheFile, nowEpochMs) {
  let raw;
  try {
    raw = fs.readFileSync(cacheFile, "utf8");
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw error;
  }
  const record = JSON.parse(raw);
  if (!record || typeof record.token !== "string" || record.token.length === 0 || !Number.isFinite(record.expiresAt)) {
    throw new TypeError("invalid token cache record");
  }
  return record.expiresAt > nowEpochMs ? record.token : null;
}

function getCachedAuthorization(cacheFile, nowEpochMs) {
  const token = duplicatedReadFreshToken(cacheFile, nowEpochMs);
  return token === null ? null : `Bearer ${token}`;
}

module.exports = { getCachedAuthorization };
""",
    encoding="utf-8",
)
