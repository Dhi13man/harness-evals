"use strict";

const fs = require("node:fs");

function readFreshToken(cacheFile, nowEpochMs) {
  let raw;
  try {
    raw = fs.readFileSync(cacheFile, "utf8");
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return null;
    }
    throw error;
  }

  const record = JSON.parse(raw);
  if (
    !record ||
    typeof record.token !== "string" ||
    record.token.length === 0 ||
    !Number.isFinite(record.expiresAt)
  ) {
    throw new TypeError("invalid token cache record");
  }
  return record.expiresAt > nowEpochMs ? record.token : null;
}

module.exports = { readFreshToken };
