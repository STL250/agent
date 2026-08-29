# Rivet architecture

Rivet is deliberately small: the model proposes actions, but every action is parsed,
bounded, executed, observed, and recorded by code in this repository. It uses no agent
framework and no server-hosted file or execution tool.

## Data flow

```text
repeated user turns
        |
      Agent -------- ContextManager ---- tool schemas ----> OpenAICompatibleClient
        ^                  ^                                     |
        |                  |                                     v
 SessionStore        tool result <---- ReAct loop <---- assistant text / tool_calls
        ^
        |
 versioned JSON
                           ^                 |
                           |                 v
                     ToolRegistry ---- Workspace ---- local files and subprocesses
```

The loop is implemented in `src/rivet/agent.py`. `Agent` owns the message history, tool
registry, diff baseline, and accumulated execution evidence for one terminal conversation.
Each model response becomes an
assistant message. Function calls are JSON-decoded and dispatched by `ToolRegistry`;
the resulting JSON is appended as a `tool` message with the original call ID. A plain
assistant message completes the current user turn, but the session remains available for
follow-up instructions. Only `/exit` ends the process; `Agent.reset()` implements `/new` by
clearing conversation state and creating a fresh tool/diff boundary.

## Responsibility map

| Requirement | Implementation |
| --- | --- |
| Conversation/session | `Agent` retains user turns, assistant messages, tool results, diff baseline, and execution evidence |
| Session persistence | `SessionStore` atomically saves and validates versioned, workspace-scoped state |
| Context bounding | `ContextManager` preserves the system prompt, first task, recent call-result units, and a deterministic checkpoint |
| Terminal presentation | `Console` in `tui.py` renders compact semantic events with ANSI and plain-text modes |
| Tool definitions | Seven JSON-schema function tools in `ToolRegistry` with local validation |
| Local execution | `Workspace` reads, searches, atomically edits, and runs bounded subprocesses |
| Output parsing | `OpenAICompatibleClient` validates JSON replies or incrementally rebuilds SSE text and indexed tool-call deltas |
| Completion evidence | `TaskState` records inspected/changed files and command outcomes; edits require a later successful check |
| Termination | Verified final text, max steps, two empty replies, or three identical call-result observations |
| Error handling | Structured tool errors, HTTP retry/backoff, timeouts, truncation, and secret redaction |

## Tools

- `list_files`: bounded traversal with noisy cache/build directories omitted.
- `read_file`: UTF-8 text with stable line numbers and binary rejection.
- `search_text`: literal, glob-aware repository search.
- `write_file`: atomic create/replace inside the workspace.
- `replace_text`: exact, count-checked atomic edit that fails safely on stale context.
- `show_diff`: unified diff against each file's first in-session state.
- `run_command`: cancellable subprocess group in the workspace with timeout, bounded output,
  stripped credential variables, a blocklist for obviously destructive/external commands,
  and before/after workspace snapshots that detect indirect file changes.

Tool schemas are sent to the model and enforced again by `ToolRegistry`. Required fields,
unknown fields, scalar types, ranges, and string sizes are rejected before approval or local
execution. Errors include a stable code, field name when relevant, and retryability hint.

## Completion evidence

The model does not decide success by text alone after editing. `TaskState` records operations
that actually succeeded. A file mutation invalidates earlier command evidence; a later command
must finish with exit code zero before the task can return `completed_verified`. The first
premature final answer receives a corrective prompt, and the second stops as
`unverified_changes`. Read-only analysis tasks may still finish without a command.
`run_command` classifies common test/build/lint commands automatically and also accepts an
explicit `purpose` of `inspect` or `verify` for project-specific checks; only successful
verification commands satisfy completion evidence.

Before each command, Rivet hashes up to 10,000 non-cache workspace files and retains a
bounded amount of original UTF-8 text. A second snapshot detects created, modified, and
deleted files, registers them in `TaskState`, and feeds their original content into
`show_diff`. A command that changes files cannot verify those same changes in the same
operation; a later successful check is required. Binary, oversized, unreadable, or
snapshot-budget-limited changes remain visible by path with an explicit unavailable/limited
marker instead of silently disappearing.

Repeated-call protection hashes both the canonical tool call and its returned observation.
This stops genuine no-progress loops while allowing a repeated read whose content changed.

## Streaming and cancellation

`OpenAICompatibleClient.complete_stream()` requests Chat Completions SSE. Text from
`delta.content` is forwarded immediately, while `delta.tool_calls` fragments are accumulated
by their numeric index; IDs, function names, and JSON argument strings are validated only
after the stream completes. Empty usage chunks are ignored, `[DONE]` and finish reasons close
the stream, and a truncated or malformed stream fails explicitly. A compatible gateway that
rejects streaming before emitting any content is retried through the existing JSON request.

The Agent does not append an assistant message until a model stream is complete. `Ctrl+C`
during a request discards partial text and records a short cancellation marker instead. If a
tool is interrupted, synthetic cancellation results complete every advertised tool-call ID,
so subsequent API requests never contain orphaned calls. `run_command` uses a process group;
Windows termination uses `taskkill /T /F`, while POSIX sends signals to the process group.
After termination, the normal workspace snapshot comparison still records partial file side
effects. A cancelled verification command can never satisfy completion evidence. At an idle
prompt, `Ctrl+C` clears the input and keeps the session open.

## Multi-turn session and context management

Launching `rivet` starts a persistent input loop. A later user
request is appended to the same history after the preceding assistant final answer, so
references such as "the function you just changed" have the required conversational context.
Each user turn receives a fresh step budget and loop-protection counters, while successful
tool evidence and the workspace diff remain session-wide. `/status`, `/diff`, `/sessions`,
`/resume`, `/new`, and `/exit` expose the essential session controls. `/resume` without an
argument loads the most recently updated valid session; an ID from `/sessions` selects a
specific one. `/new` resets the Agent and creates a new `ToolRegistry`, so conversation
history and the in-memory diff baseline do not leak across conversations.

`SessionStore` atomically updates one versioned JSON file after each completed turn under
`.rivet/sessions`, which is ignored by Git. It persists provider messages, exact internal
operation counters, command evidence, and the original file baselines needed by `/diff`.
The primary system prompt is reconstructed rather than saved because it contains the local
workspace path; API credentials and endpoint configuration are never part of session state.
A hash of the normalized workspace path prevents a session copied from another project from
being resumed accidentally. Saved identities of tracked files are checked during restore;
changes made while Rivet was closed are registered as new mutations and invalidate older
verification evidence. Malformed, oversized, path-escaping, unsupported-version, and
cross-workspace files are rejected. Transcript-only files from version 1.1 are upgraded with
an explicitly limited diff state.

Character count is used as a provider-neutral upper-bound heuristic. When history grows
past the configured limit, old messages are grouped into semantic units. An assistant
message containing tool calls and all matching tool results are indivisible; this avoids
orphaned `tool_call_id` values. Old units become a short factual checkpoint, while recent
units remain verbatim. Tool output itself is head-tail truncated so diagnostics at both
ends survive.

## Failure policy

| Failure | Response |
| --- | --- |
| 408/409/429/5xx or transient network error | Exponential backoff, then a sanitized `ModelError` |
| Malformed model JSON or schema | Stop with an explicit protocol error |
| Malformed/unknown tool call | Return `{ok:false,error:...}` to the model so it can recover |
| Path traversal or binary text read | Reject before access |
| Command timeout | Return partial output and `timed_out:true` |
| User cancellation | Stop the active turn, preserve completed evidence, save the session, and return to input |
| Stale exact edit | Reject; model must re-read instead of corrupting the file |
| Final answer after an unverified edit | Reprompt once, then stop as `unverified_changes` |
| Empty model reply | One corrective reprompt, then stop |
| Repeating loop | Stop on the third identical call-result observation |
| Runaway task | Hard `max_steps` limit |

## Security boundary and trade-offs

Path tools resolve symlinks and reject locations outside the selected workspace. API keys
are sent only in the model HTTP header, never logged, and credential-shaped environment
variables are removed from subprocesses. `safe`, `ask`, and read-only `never` approval
modes make the write boundary visible.

`run_command` is intentionally capable and is not an OS sandbox. Destructive commands remain
blocked, while package installation, network access, and Git history/branch mutations require
approval even in `safe` mode. For hostile repositories,
run Rivet in a disposable container or VM and use `--approval ask`. The project favors a
small transparent core over provider-specific features. Chat Completions is widely
supported by compatible gateways; a second protocol can be added behind the `ModelClient`
interface without changing the agent loop.

