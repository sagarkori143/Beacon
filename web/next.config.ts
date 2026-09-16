import path from "node:path";

import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // `web/` has its own lockfile inside a repo that also has Python packaging at
  // the root, which leaves Next guessing where the workspace starts. Say so.
  turbopack: { root: path.resolve(".") },
};

export default config;
