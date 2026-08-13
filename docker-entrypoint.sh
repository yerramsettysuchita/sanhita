#!/bin/sh
# Seed the store when the host has mounted an empty disk over it.
#
# The compiled rulebook, its 183 certifications and the audit ledger are baked
# into the image at build time. A host that persists `.sanhita` mounts a fresh
# block device over exactly that path, and a fresh block device is empty, so the
# mount hides the very thing it was added to preserve. The container starts
# cleanly, answers its health check, and serves a product with zero rules.
#
# This was not theoretical. `render.yaml` declares a disk at `/app/.sanhita`,
# and running the image with an empty mount there reported `"rules": 0`.
#
# So the image keeps a pristine copy at a path no mount will cover, and copies
# it in only when the live store has no rulebook. An existing store is never
# touched, which matters because that store holds certifications a named person
# signed and evidence a firm uploaded.
set -e

DIST=/app/.sanhita-dist
LIVE=/app/.sanhita

if [ -d "$DIST" ] && [ ! -f "$LIVE/rules.json" ]; then
  echo "store is empty, seeding it from the image"
  mkdir -p "$LIVE"
  cp -R "$DIST"/. "$LIVE"/
fi

exec sanhita serve --host 0.0.0.0 --port "${PORT:-8000}"
