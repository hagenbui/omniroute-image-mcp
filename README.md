# OmniRoute Image MCP

Local stdio MCP connector for Claude Desktop, Claude Code and OpenCode. Python standard library only. Version 1.6.1.

Requests go through your OmniRoute endpoint. OmniRoute uses its connected provider account to generate images. This repo contains no API keys, provider tokens, generated images or live jobs. Source launch.sh is the local version; the active installation now uses the hosted launcher described below.

## Current installation: hosted on the worker host (2026-11-17)

The working Mac launcher now runs bridge.py over SSH to the worker host. Generation, persistent queue and primary image files live at ~/services/image-mcp on the worker host. OmniRoute is reached directly there on loopback port20128. The Mac bridge uploads explicit local references, returns inline images, caches returned images at their original Mac output location, and recovers old local tickets exactly.

The shared queue runs two jobs and holds up to32 waiting, across all clients using this server installation. Client connections can disconnect while workers continue on the worker host. This is a private SSH deployment, not a public HTTPS MCP URL: a native Claude web/mobile connection still needs an appropriate client bridge. A Mac or another device running a compatible SSH client is required to make tool calls.

### Add to another Mac

Install server.py, bridge.py, and launch-remote.sh in a private runtime directory, create image.env with local output directory settings, configure SSH alias worker-host for your own authorized key, and use launch-remote.sh instead of launch.sh in the client examples below. The new computer needs SSH authorization to the worker host. Do not copy private SSH keys or image.env into Git.

The bridge sources local image.env for the old image output directory and optional OMNIROUTE_IMAGE_SSH_HOST (defaults to worker-host). The provider API key is kept in the worker host's private image.env; a new bridge-only computer does not require that key locally.

### Rollback on the existing Mac

The working runtime keeps launch-local.sh plus a dated backup. Replacing the live launch.sh with launch-local.sh restores the local implementation. Do this only when client activity is idle, then reconnect clients. Remote queued jobs must finish or be explicitly dealt with; do not regenerate them during rollback. Old local and remote tickets have different storage locations.

### Verified

Remote core:30 tests passed with fake credentials and a localhost fake provider. Bridge:8 tests passed, including old recovery and atomic local cache. Real SSH initialization/tool discovery passed for Desktop, Code and OpenCode; exact old-image retrieval passed. Independent review found no blocking issues. A paid live reference edit was not run because automatic approval review rejected image export/quota use.

## New Claude Desktop setup on this Mac

The working installation is at ~/Library/Application Support/openai-image-mcp.

Merge this entry into mcpServers in ~/Library/Application Support/Claude/claude_desktop_config.json, preserving other entries. Replace /Users/YOUR_USER with your actual home path; JSON does not expand ~ or $HOME.

~~~json
{
  "mcpServers": {
    "openai-images": {
      "command": "/bin/sh",
      "args": [
        "/Users/YOUR_USER/Library/Application Support/openai-image-mcp/launch.sh",
        "claude-desktop"
      ]
    }
  }
}
~~~

Fully quit and reopen Claude when image work is idle. A new chat uses the existing connection; it needs no separate installation.

For Claude Code:

~~~sh
claude mcp add --scope user openai-images -- /bin/sh "$HOME/Library/Application Support/openai-image-mcp/launch.sh" claude-code
claude mcp get openai-images
~~~

If already registered, inspect the existing entry before replacing it.

For OpenCode, merge into your existing configuration:

~~~json
{
  "mcp": {
    "lcl-openai-images": {
      "type": "local",
      "command": [
        "/bin/sh",
        "/Users/YOUR_USER/Library/Application Support/openai-image-mcp/launch.sh",
        "opencode"
      ],
      "timeout": 660000
    }
  }
}
~~~

## Install on another Mac

Requires /usr/bin/python3 and access to OmniRoute supporting /v1/images/generations and /v1/images/edits.

1. Clone or copy this repository.
2. For a fresh installation, copy server.py and launch.sh to ~/Library/Application Support/openai-image-mcp/.
3. Copy image.env.example there as image.env, set your key and endpoint, then run:

~~~sh
chmod 600 "$HOME/Library/Application Support/openai-image-mcp/image.env"
~~~

4. Configure your client as above.

The example loopback URL works only with a tunnel on that machine. Establish your own secure route or tunnel, for example:

~~~sh
ssh -N -T -o ExitOnForwardFailure=yes -L 127.0.0.1:20129:127.0.0.1:20128 YOUR_OMNIROUTE_SSH_HOST
~~~

Keep it running or manage it as a service. The SSH host must reach OmniRoute on its localhost port 20128.

Remote endpoints require HTTPS; HTTP is allowed only for loopback. A proxied public endpoint may cut off long generations; the existing installation uses SSH to avoid that cutoff.

## Tools and queue

- generate_image: new image from a prompt; omit IDs.
- edit_reference_image: prompt plus reference_image (accessible file path or PNG/JPEG/WebP data URL), or exact reference_ticket returned in THIS chat.
- fetch_image: retrieve an existing result by its exact ticket.
- create_image_session and recover_saved_image: compatibility tools for explicit-ID clients.

Long calls return a ticket while a detached worker continues. Never resubmit merely because a job is queued or slow; fetch its exact ticket. Images and metadata are saved under the output directory, separated by client/session.

Two workers run across clients sharing one installed runtime directory. Extra jobs queue FIFO, up to 32 waiting. Separate installations have independent queues. OmniRoute chooses provider accounts; two workers do not guarantee separate-account routing.

## Development and updates

~~~sh
PYTHONDONTWRITEBYTECODE=1 python3 test_server.py
~~~

Tests use temporary output, fake credentials, mocked responses and a localhost-only provider. No paid generation. Subprocess tests need permission to bind a localhost socket. Queue/lock artifacts are ignored by Git.

Editing this repo does not change the live installation. Before replacing installed files, back up the existing files and client configs and acquire an exclusive lock on the live runtime's .generation.lock. Wait for every active/queued worker to finish. Keep image.env, keys and outputs private. Do not overwrite the live runtime while jobs run.

The launcher is Mac-specific (/usr/bin/python3); other operating systems need an adapted launcher and configuration.

## Limits

- This is a local stdio connector, not a remote HTTP MCP service. Web/phone access requires a suitable bridge to the host computer.
- Reference paths must be readable on that host. Cowork VM /mnt paths need a host copy or image data URL.
- Client namespaces and tickets prevent accidental mixing, not access by another process running as the same OS user.
- Jobs survive client disconnects, but reboot or worker termination can leave unknown outcomes; jobs are not automatically replayed.
- Upstream timeouts may still be billed. Usage is recorded only when supplied by the provider.
- codex/gpt-5.6-sol is the native model behind the older codex/gpt-5.6-sol-image catalog alias. The deployed per-account inventory filter rejected that alias; do not restore it without verification.

## SSH idle-disconnect fix (2026-11-17)

The Mac MCP bridge stays running without holding an idle SSH session. Discovery and ping are local; each actual remote tool call opens its own SSH connection and closes it afterward. A connection failure returns a tool error and does not replay the paid request. The next call opens a fresh connection. Generation requests have known IDs before submission so failures can include an exact recovery ticket. Real tests verified two successive SSH calls and idle ping for all three clients; eight bridge tests pass. New paid generation was not part of this verification.

## SSH response deadline (2026-11-18)

Each tool connection has a50-second overall deadline, including connection setup and response transfer. Expiry closes only the local SSH child; already submitted remote workers continue. Errors retain the exact known ticket and never resubmit. Nine bridge tests pass; focused review also simulated a real sleeping transport and confirmed deadline exit with no retry.
