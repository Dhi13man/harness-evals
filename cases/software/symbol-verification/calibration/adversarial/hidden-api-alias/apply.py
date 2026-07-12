#!/usr/bin/env python3
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
store = workspace.joinpath("token-store.js")
store.write_text(
    store.read_text(encoding="utf-8")
    + """
Object.defineProperty(module.exports, "getToken", {
  enumerable: false,
  value: readFreshToken,
});
""",
    encoding="utf-8",
)
workspace.joinpath("session.js").write_text(
    """"use strict";

const tokenStore = require("./token-store");

function getCachedAuthorization(cacheFile, nowEpochMs) {
  const token = tokenStore.getToken(cacheFile, nowEpochMs);
  return token === null ? null : `Bearer ${token}`;
}

module.exports = { getCachedAuthorization };
""",
    encoding="utf-8",
)
