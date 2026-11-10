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
    try { await fetch(`${base}/agent`); return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`Test server failed to start: ${logs}`);
});
after(() => server?.kill("SIGTERM"));

function agent(prompt) {
  return new HttpAgent({ url: `${base}/agent`, threadId: crypto.randomUUID(), initialMessages: [{id: crypto.randomUUID(), role: "user", content: prompt}] });
}

test("HttpAgent consumes streamed answer; application can restore client messages", async () => {
  const a = agent("hello");
  const events = [];
  await a.runAgent({runId: "text"}, {onEvent: ({event}) => events.push(event)});
  assert.equal(events.at(-1).type, "RUN_FINISHED");
  assert.equal(a.messages.at(-1).content, "Answer: hello");
  // Persistence is the application's choice, not another adapter endpoint.
  const saved = JSON.parse(JSON.stringify({messages: a.messages, state: a.state}));
  const restored = new HttpAgent({url: `${base}/agent`, threadId: a.threadId, initialMessages: saved.messages, initialState: saved.state});
  assert.deepEqual(fromAgUiMessages(restored.messages), fromAgUiMessages(a.messages));
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
  const events = [];
  await a.runAgent({}, {onEvent: ({event}) => events.push(event), onRunFinishedEvent: ({event}) => {outcome = event.outcome;}});
  assert.deepEqual(events.slice(-3).map(e => e.type), ["MESSAGES_SNAPSHOT", "STATE_SNAPSHOT", "RUN_FINISHED"]);
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

test("useAgUiRuntime executes parallel frontend tools and continues with all results", async () => {
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
    runtime.thread.append("frontend-parallel");
  });
  for (let i = 0; i < 100; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
    const state = runtime.thread.getState();
    // Receiving the final text chunk precedes RUN_FINISHED and React's idle state.
    if (!state.isRunning && state.messages.some(m => m.content.some(p => p.type === "text" && p.text === "Answer: frontend-parallel"))) break;
  }
  const snapshot = runtime.thread.getState();
  await act(async () => { unregister(); root.unmount(); });
  assert.deepEqual(errors, []);
  assert.equal(executed, 2);
  assert.equal(snapshot.isRunning, false);
  assert.ok(snapshot.messages.some(m => m.content.some(p => p.type === "text" && p.text === "Answer: frontend-parallel")));
});

test("aborting the HTTP stream releases execution without a cancel endpoint", async () => {
  const a = agent("cancel");
  let started;
  const ready = new Promise(resolve => {started = resolve;});
  const running = a.runAgent({}, {onToolCallEndEvent: () => started()}).catch(() => {});
  await ready;
  a.abortRun();
  await running;
  for (let i = 0; i < 100; i++) {
    const next = new HttpAgent({url: `${base}/agent`, threadId: a.threadId, initialMessages: [{id: crypto.randomUUID(), role: "user", content: "hello"}]});
    try {
      await next.runAgent({});
      assert.equal(next.messages.at(-1).content, "Answer: hello");
      return;
    } catch { await new Promise(resolve => setTimeout(resolve, 20)); }
  }
  assert.fail("thread was not released after abort");
});

test("only the AG-UI run endpoint is exposed", async () => {
  assert.equal((await fetch(`${base}/threads/t/history`)).status, 404);
  assert.equal((await fetch(`${base}/threads/t/runs/r/events`)).status, 404);
  assert.equal((await fetch(`${base}/threads/t/runs/r/cancel`, {method: "POST"})).status, 404);
});

test("reasoning stays distinct from assistant answer text", async () => {
  const a = agent("reasoning");
  const events = [];
  await a.runAgent({}, {onEvent: ({event}) => events.push(event)});
  assert.ok(events.some(e => e.type === "REASONING_MESSAGE_CONTENT"));
  assert.equal(a.messages.find(m => m.role === "reasoning").content, "Think first");
  assert.equal(a.messages.at(-1).content, "Answer: reasoning");
});

test("activity replacement and patches are consumed by the official reducer", async () => {
  const a = agent("activity");
  await a.runAgent({});
  const activity = a.messages.find(m => m.role === "activity");
  assert.equal(activity.activityType, "progress");
  assert.deepEqual(activity.content, {count: 3});
});


test("native child steps do not collide with the parent's AG-UI steps", async () => {
  const a = agent("subagent");
  const events = [];
  await a.runAgent({}, {onEvent: ({event}) => events.push(event)});
  assert.deepEqual(events.filter(e => e.type === "STEP_STARTED").map(e => e.stepName), ["1", "2"]);
  assert.deepEqual(events.filter(e => e.type === "STEP_FINISHED").map(e => e.stepName), ["1", "2"]);
  assert.equal(events.at(-1).type, "RUN_FINISHED");
  assert.equal(a.messages.at(-1).content, "Answer: subagent");
  assert.equal(a.messages.filter(m => m.role === "tool").length, 1);
  assert.equal(a.messages.find(m => m.role === "tool").content, "child result");
});


test("invalid interrupt resumes end with RUN_ERROR, not an HTTP error", async () => {
  const a = agent("interrupt");
  const events = [];
  await a.runAgent({resume: [{interruptId: "unknown", status: "resolved", payload: true}]}, {onEvent: ({event}) => events.push(event)}).catch(() => {});
  assert.deepEqual(events.map(e => e.type), ["RUN_STARTED", "RUN_ERROR"]);
});
