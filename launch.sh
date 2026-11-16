#!/bin/sh
set -eu
MCP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$MCP_DIR/image.env"
case "${1:-}" in
  claude-desktop|claude-code|opencode) export OMNIROUTE_IMAGE_CLIENT="$1" ;;
  *) echo 'Pass client: claude-desktop, claude-code, or opencode' >&2; exit 2 ;;
esac
exec /usr/bin/python3 "$MCP_DIR/server.py"
