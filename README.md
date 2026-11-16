# OmniRoute Image MCP

Local stdio MCP connector for Claude Desktop, Claude Code and OpenCode. Python standard library only. Version 1.6.1.

Requests go through your OmniRoute endpoint. OmniRoute uses its connected provider account to generate images. This repo contains no API keys, provider tokens, generated images or live jobs.

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
