import assert from "node:assert/strict";
import { before, after, beforeEach, afterEach, test } from "node:test";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";
import { HttpAgent } from "@ag-ui/client";
import { fromAgUiMessages } from "@assistant-ui/react-ag-ui";

// SDK interoperability checks, not a local implementation of the upstream TCK.
// Reject compatibility warnings rather than silently relying on client repairs.
let warnings;
beforeEach(t => {
  warnings = [];
  t.mock.method(console, "warn", (...args) => warnings.push(args.map(String).join(" ")));
});
afterEach(() => assert.deepEqual(warnings, [], "AG-UI client reported compatibility warnings"));

let server, base, logs = "";
before(async () => {
  const socket = createServer();
  await new Promise(resolve => socket.listen(0, "127.0.0.1", resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  base = `http://127.0.0.1:${port}`;
  const python = process.env.PYTHON || fileURLToPath(new URL("../../../../.venv/bin/python", import.meta.url));
  server = spawn(python, [fileURLToPath(new URL("../client_server.py", import.meta.url)), String(port)], { stdio: ["ignore", "ignore", "pipe"] });
  server.stderr.on("data", data => { logs += data.toString(); });
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${base}/threads/probe/history`)).ok) return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`Test server failed to start: ${logs}`);
});
after(() => server?.kill("SIGTERM"));

function agent(prompt) {
  return new HttpAgent({ url: `${base}/agent`, threadId: crypto.randomUUID(), initialMessages: [{id: crypto.randomUUID(), role: "user", content: prompt}] });
}

test("HttpAgent consumes streamed answer; assistant-ui restores identical history", async () => {
  const a = agent("hello");
  const events = [];
  await a.runAgent({runId: "text"}, {onEvent: ({event}) => events.push(event)});
  assert.equal(events.at(-1).type, "RUN_FINISHED");
  assert.equal(a.messages.at(-1).content, "Answer: hello");
  const history = await (await fetch(`${base}/threads/${a.threadId}/history`)).json();
  assert.deepEqual(history.messages, a.messages);
  assert.deepEqual(fromAgUiMessages(history.messages), fromAgUiMessages(a.messages));
});

test("backend tool has parent, result and final answer", async () => {
  const a = agent("backend");
  const events = [];
  await a.runAgent({}, {onEvent: ({event}) => events.push(event)});
  const start = events.find(e => e.type === "TOOL_CALL_START");
  assert.ok(a.messages.some(m => m.id === start.parentMessageId));
  assert.equal(a.messages.find(m => m.role === "tool").content, "12:00 UTC");
  assert.equal(events.filter(e => e.type === "RUN_FINISHED").length, 1);
  assert.equal(a.messages.at(-1).content, "Answer: backend");
});

test("frontend call terminates, accepts tool result and continues without a fake user", async () => {
  const a = agent("frontend");
  const tools = [{name: "browser", description: "Browser action", parameters: {type: "object"}}];
  await a.runAgent({tools});
  const call = a.messages.find(m => m.toolCalls)?.toolCalls[0];
  assert.equal(call.function.name, "browser");
  assert.equal(a.messages.filter(m => m.role === "tool").length, 0);
  a.addMessage({id: crypto.randomUUID(), role: "tool", toolCallId: call.id, content: "client result"});
  await a.runAgent({tools});
  assert.equal(a.messages.at(-1).content, "Answer: frontend");
  assert.equal(a.messages.filter(m => m.role === "user").length, 1);
});

test("interrupt outcome and resume are understood by the protocol client", async () => {
  const a = agent("interrupt");
  let outcome;
  await a.runAgent({}, {onRunFinishedEvent: ({event}) => {outcome = event.outcome;}});
  assert.equal(outcome.type, "interrupt");
  await a.runAgent({resume: [{interruptId: outcome.interrupts[0].id, status: "resolved", payload: true}]});
  assert.equal(a.messages.at(-1).content, "Answer: interrupt");
});

test("state snapshot and RFC6902 delta reach client state", async () => {
  const a = agent("state");
  await a.runAgent({});
  assert.deepEqual(a.state, {count: 2});
});

test("provider errors do not turn into success", async () => {
  const a = agent("error");
  const events = [];
  try { await a.runAgent({}, {onEvent: ({event}) => events.push(event)}); } catch {}
  assert.equal(events.at(-1).type, "RUN_ERROR");
  assert.equal(events.filter(e => e.type === "RUN_FINISHED").length, 0);
});

test("useAgUiRuntime executes frontend tools and automatically continues the thread", async () => {
  const React = await import("react");
  const {create, act} = await import("react-test-renderer");
  const {useAgUiRuntime} = await import("@assistant-ui/react-ag-ui");
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  let runtime, root, executed = 0;
  const errors = [];
  const a = new HttpAgent({url: `${base}/agent`, threadId: crypto.randomUUID()});
  function Harness() {
    runtime = useAgUiRuntime({agent: a, onError: error => errors.push(error)});
    return null;
  }
  await act(async () => { root = create(React.createElement(Harness)); });
  let unregister;
  await act(async () => {
    unregister = runtime.registerModelContextProvider({getModelContext: () => ({tools: {
      browser: {description: "Browser action", parameters: {type: "object", properties: {}}, execute: async () => {executed++; return "browser value";}}
    }})});
    runtime.thread.append("frontend");
  });
  for (let i = 0; i < 100; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
    const state = runtime.thread.getState();
    // Receiving the final text chunk precedes RUN_FINISHED and React's idle state.
    if (!state.isRunning && state.messages.some(m => m.content.some(p => p.type === "text" && p.text === "Answer: frontend"))) break;
  }
  const snapshot = runtime.thread.getState();
  await act(async () => { unregister(); root.unmount(); });
  assert.deepEqual(errors, []);
  assert.equal(executed, 1);
  assert.equal(snapshot.isRunning, false);
  assert.ok(snapshot.messages.some(m => m.content.some(p => p.type === "text" && p.text === "Answer: frontend")));
});

test("aborting the HTTP stream cancels server execution and persists cancellation", async () => {
  const a = agent("cancel");
  let started;
  const ready = new Promise(resolve => {started = resolve;});
  const running = a.runAgent({runId: "cancel-run"}, {onToolCallEndEvent: () => started()}).catch(() => {});
  await ready;
  a.abortRun();
  await running;
  let history;
  for (let i = 0; i < 100; i++) {
    history = await (await fetch(`${base}/threads/${a.threadId}/history`)).json();
    if (history.runs["cancel-run"].done) break;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.equal(history.runs["cancel-run"].outcome.code, "CANCELLED");
  assert.equal(history.runs["cancel-run"].outcome.type, "RUN_ERROR");
});

test("disconnecting a duplicate POST does not cancel the owning run", async () => {
  const a = agent("cancel");
  const body = {threadId: a.threadId, runId: "shared", messages: a.messages, tools: [], context: [], state: {}, forwardedProps: {}};
  const ownerAbort = new AbortController();
  const owner = await fetch(`${base}/agent`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body), signal: ownerAbort.signal});
  assert.equal(owner.status, 200);
  const ownerReader = owner.body.getReader();
  await ownerReader.read();
  const duplicateAbort = new AbortController();
  const duplicate = await fetch(`${base}/agent`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body), signal: duplicateAbort.signal});
  const reader = duplicate.body.getReader();
  await reader.read();
  duplicateAbort.abort();
  await new Promise(resolve => setTimeout(resolve, 40));
  const history = await (await fetch(`${base}/threads/${a.threadId}/history`)).json();
  assert.equal(history.runs.shared.done, false);
  await fetch(`${base}/threads/${a.threadId}/runs/shared/cancel`, {method: "POST"});
  ownerAbort.abort();
});

test("reasoning stays distinct from assistant answer text", async () => {
  const a = agent("reasoning");
  const events = [];
  await a.runAgent({}, {onEvent: ({event}) => events.push(event)});
  assert.ok(events.some(e => e.type === "REASONING_MESSAGE_CONTENT"));
  assert.equal(a.messages.find(m => m.role === "reasoning").content, "Think first");
  assert.equal(a.messages.at(-1).content, "Answer: reasoning");
});

test("activity replacement and patches produce the same history as the official reducer", async () => {
  const a = agent("activity");
  await a.runAgent({});
  const activity = a.messages.find(m => m.role === "activity");
  assert.equal(activity.activityType, "progress");
  assert.deepEqual(activity.content, {count: 3});
  const history = await (await fetch(`${base}/threads/${a.threadId}/history`)).json();
  assert.deepEqual(history.messages, a.messages);
});
