# Changelog

## 0.2.0

- Delegation, which only the human turns on: with `[delegation] lead_speaks_for_human = true` in `bridge.toml`, a BRIEF or REPLY from a lead reaches builders as the human's instruction.
  The wake notice, the header above the messages and the generated instructions all say so, so a builder acts without asking the human again.
  Messages from other members stay information, and each agent's own permission rules and the escalation rules still apply.
- New projects get the `[delegation]` table in `bridge.toml`, switched off.

## 0.1.0

- Threads of numbered markdown messages with front matter, written so no reader sees half a message and no two writers share a number.
- A working agreement in `bridge.toml`: roles, the kinds each role may send, what goes to the human, and the house rules.
- Each agent's instructions generated from the agreement, as a skill, an `AGENTS.md` block or plain markdown.
- Session discovery for the supported agents and tmux panes, named members, and threads that pair them, with a picker in the terminal.
- Wake adapters for an open session, a saved session, a queued thread, a tmux pane and your own command, with polling as the fallback and plugins through entry points.
- A check that refuses messages and text attachments that look like secrets, and attachments outside the project.
- A read-only viewer on 127.0.0.1 that shows each thread with its images inline.
