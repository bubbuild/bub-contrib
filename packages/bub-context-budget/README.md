# bub-context-budget

Proactive context-budget guard for Bub's builtin agent loop.

## What It Provides

- Bub plugin entry point: `context-budget`
- A `before_llm_call` hook that runs after other request modifiers where possible
- Provider-reported token usage combined with an incremental message estimate
- A configurable `tiktoken` fallback when no matching usage baseline exists
- A fixed 16,384-token reserve below the configured context limit
- A system instruction asking the model to call `tape.handoff` before continuing when the budget is exceeded

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-context-budget"
```

You can also install it with Bub:

```bash
bub install bub-context-budget@main
```

## Configuration

Add the plugin section to the Bub config file:

```yaml
context-budget:
  max_context_tokens: 200000
  encoding_name: o200k_base
```

The defaults are equivalent to the example above. Environment variables are also supported:

- `BUB_CONTEXT_BUDGET_MAX_CONTEXT_TOKENS`
- `BUB_CONTEXT_BUDGET_ENCODING_NAME`

Use an encoding that approximates the configured model's tokenizer. `o200k_base` is the default because Bub model identifiers may refer to providers that `tiktoken.encoding_for_model` cannot resolve.

## Runtime Behavior

After each successful builtin agent-loop LLM call, the plugin records the provider-reported total usage and a digest of the messages covered by that usage. On the next call, a matching model, tool set, and message prefix uses that real total plus a `tiktoken` estimate of only the newly appended messages. This follows Pi's usage-baseline approach without retaining a second copy of the conversation in memory.

If usage is unavailable, the model or tool set changed, or the message prefix no longer matches, the plugin estimates the full message list. This fallback count covers the serialized messages but not provider-added tool schemas or protocol framing.

The handoff threshold is `max_context_tokens - 16384`, leaving fixed headroom for the injected instruction, provider framing, and the handoff response. With the default 200,000-token limit, the threshold is 183,616 tokens. A count equal to the threshold is allowed. A count above it adds a high-priority, idempotent system instruction telling the model to call `tape.handoff` with a concise continuity summary before doing any other work.

Bub normalizes streaming and non-streaming completion usage into `LlmCallResult.usage`; the same data backs `StreamState` and `AsyncStreamEvents` and is later persisted to tape. The plugin consumes the normalized hook result directly and stores its lightweight baseline in the current `TurnState`. A fresh state falls back to full estimation until its first successful LLM call establishes a baseline.

The plugin only modifies the outgoing request. It does not call `tape.handoff` itself, and it requires that tool to be available to the model. Alternate `run_model` providers that bypass Bub's builtin agent-loop interception hooks are outside its scope.
