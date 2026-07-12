#!/usr/bin/env python3
import json
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
module_dir = workspace.joinpath("node_modules", "fresh-token-wrapper")
module_dir.mkdir(parents=True)
module_dir.joinpath("index.js").write_text(
    """"use strict";
module.exports = require("../../token-store").readFreshToken;
""",
    encoding="utf-8",
)
workspace.joinpath("package.json").write_text(
    json.dumps({"dependencies": {"fresh-token-wrapper": "1.0.0"}}) + "\n",
    encoding="utf-8",
)
workspace.joinpath("session.js").write_text(
    """"use strict";

const readFreshToken = require("fresh-token-wrapper");

function getCachedAuthorization(cacheFile, nowEpochMs) {
  const token = readFreshToken(cacheFile, nowEpochMs);
  return token === null ? null : `Bearer ${token}`;
}

module.exports = { getCachedAuthorization };
""",
    encoding="utf-8",
)
