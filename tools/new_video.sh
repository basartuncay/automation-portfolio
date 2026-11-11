#!/usr/bin/env bash
# Set up demo/video for a project from the generic template. node_modules is shared with an existing install.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$here/../projects/$1/demo/video"
mkdir -p "$dest/public" "$dest/src"
cp "$here"/video-template/{package.json,package-lock.json,tsconfig.json,remotion.config.ts,.gitignore,.prettierrc,eslint.config.mjs} "$dest/"
cp "$here"/video-template/src/{index.ts,Root.tsx,Composition.tsx} "$dest/src/"
[ -e "$dest/node_modules" ] || ln -s "$here/../projects/sync-hub/demo/video/node_modules" "$dest/node_modules"
echo "ready: $dest"
