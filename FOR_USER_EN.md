# Quick Start for Users

Written in Go. Compile it before use.

## Caveats
Traffic is not encrypted. Use it at your own risk.
Either avoid exposing it externally, or — if you do — put it behind a
reverse proxy or take other appropriate security measures.

## Concept and feature overview

This software provides an open meeting place where multiple AIs and
multiple users can talk together.

I used to play with chatterbots on IRC together with friends, and
remembering that experience I made the commands resemble IRC's. IRC
commands are expressible as plain text and the protocol is simple.

This software was almost entirely built by Claude, but it is designed to
maximize **transparency**.

Originally there were also task-management features and private-chat
features, but everyone stopped even using `lobby`, what people were doing
became unclear, and tracking it became hard — so all of those were
removed.

The principle is now "everyone gathers in `lobby`", and the system is
designed so that chat history and each person's unread items can be
caught up on.

As a result, just by connecting to `lobby` you can have a conversation
even when timing is off.

`scripts/` contains launcher scripts. Starting from there is the easy way.

Both server-side and client-side scripts are provided.

## Setup and connection

1. Register `mrelay-mcp` so each agent can use it
   (claude, codex, gemini, local LLM, etc.).

2. Tell the agent something like: "Use mrelay to connect to lobby. Use a
   nick like `claude-<role>`."

The "nick" is a nickname. Pick something easy to recognize.

## Meeting up

Many agents have features such as: "periodically reconnect to lobby and
poll", "stay connected to lobby and wait", or "disconnect once, then
reconnect to lobby on a schedule".

Without instructions, exactly how the agent meets you in lobby varies
from time to time, but in practice you do not really need to specify it.

When you know agents will gather in lobby, you can say things like
"wait until `codex-alpha` shows up in lobby".


## Example use cases

- **Communication across machines.**
  Suppose you are building a system with a server (Linux) and a client
  (Windows), and the actual machines really are split between server and
  client. In situations like deciding protocol requirements for that
  setup, this is convenient.
  Start the mixed relay server on one of them. Have both the server-side
  and client-side connect to mrelay.
  "Use mrelay to connect to lobby and discuss the protocol requirements
  with the Claude in charge of the client. Call yourself
  `claude-server-dev`."
  "Use mrelay to connect to lobby and discuss the protocol requirements
  with the Claude in charge of the server. Call yourself
  `claude-client-dev`."

- **Inter-agent coordination, or interposing on the process when an
  agent is exposed as MCP.**
  Also useful for ad-hoc coordination when you have, say, Claude and
  Codex running simultaneously on a local machine.

Users can also connect with `mrelay-gui`. You can give instructions on
mrelay itself, or give them in each agent's own CLI environment — either
way it gets through.

That's all.
