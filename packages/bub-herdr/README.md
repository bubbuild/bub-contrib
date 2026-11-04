# bub-herdr

Reports `bub chat` status to herdr so the Herdr app shows Bub as an agent: working, done, error and idle, like Claude Code or Codex.

## What It Provides

- Bub plugin entry point: `herdr`
- `load_state`, `before_tool_call`, `save_state` and `on_error` hooks that write OSC 7501 program-status sequences to the terminal

## Installation

```bash
bub install bub-herdr@main
```

## Runtime Behavior

The plugin is active only when both hold:

- the command is `bub chat`
- it runs in a herdr-core terminal (`HC_TERMINAL_ID` is set)

Otherwise it writes nothing, so `bub gateway`, `bub run` and other terminals are unaffected.

| When | Reported |
| --- | --- |
| `bub chat` starts | `idle` |
| a CLI turn starts | `working` |
| a tool is called | `working`, message: the tool name |
| a CLI turn ends | `done`, message: the first line of the reply, when Bub passes one to `save_state` |
| an error occurs | `error`, message: the stage and the error |
| Bub exits | status cleared |

Sequences have the form `ESC ] 7501 ; state=<state>:app=bub[:msg=<base64>] ESC \`. Messages are base64-encoded, stripped of control characters, and cut to 2048 characters. They go to `/dev/tty`, so they never mix with redirected output.

## Usage Notes

- To name Bub and give it an icon in the Herdr app, add an entry to `~/.config/herdr-app/programs.json`:

  ```json
  { "programs": { "bub": { "name": "Bub", "match": ["bub"], "run": ["bub", "chat"] } } }
  ```

  With `run`, Bub also appears in the app's new-terminal menu.
- Bub has no permission prompts, so the plugin never reports `blocked`.
