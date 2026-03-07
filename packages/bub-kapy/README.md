# bub-kapy

Kapybara integration for `bub`.

`bub-kapy` is a real bub model plugin: it shells out to a configurable Kapybara
runtime, persists per-session thread ids, and optionally exposes bundled
`bub_skills` into the runtime workspace.

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-kapy"
```

## Usage

This plugin registers a `kapy` model to `bub`.

```bash
BUB_KAPY_COMMAND="kapybara chat --json -" bub --model kapy "Hello Kapybara!"
```

## Configuration

Environment variables use the `BUB_KAPY_` prefix:

- `BUB_KAPY_COMMAND`: shell-style command used to invoke Kapybara
- `BUB_KAPY_MODEL`: optional `--model` override
- `BUB_KAPY_YOLO_MODE`: when `true`, appends `--dangerously-bypass-approvals-and-sandbox`
- `BUB_KAPY_PROMPT_MODE`: `stdin` or `argv`
- `BUB_KAPY_RESUME_FORMAT`: command fragment used when a prior thread id exists
- `BUB_KAPY_COPY_SKILLS`: when `true`, symlinks `bub_skills` into `.agents/skills`

## Runtime behavior

- Stores session thread ids in `.bub-kapy-threads.json` under the active workspace
- Removes a leading JSON metadata line from stdout when it contains a thread id
- Returns combined stdout/stderr so bub can surface backend failures clearly
