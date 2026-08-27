# Rivet architecture

Rivet is deliberately small: the model proposes actions, but every action is parsed,
bounded, executed, observed, and recorded by code in this repository. It uses no agent
framework and no server-hosted file or execution tool.

## Data flow

```text
user task
   |
ContextManager ---- tool schemas ----> OpenAICompatibleClient
   ^                                      |
   |                                      v
tool result <---- Agent loop <---- assistant text / tool_calls
   ^                 |
   |                 v
ToolRegistry ---- Workspace ---- local files and subprocesses
```

The loop is implemented in `src/rivet/agent.py`. Each model response becomes an
assistant message. Function calls are JSON-decoded and dispatched by `ToolRegistry`;
the resulting JSON is appended as a `tool` message with the original call ID. A plain
assistant message terminates successfully.

## Responsibility map

| Requirement | Implementation |
| --- | --- |
| Conversation/context | `ContextManager` preserves the system prompt, task, recent call-result units, and a deterministic checkpoint |
| Tool definitions | Six JSON-schema function tools in `ToolRegistry` |
| Local execution | `Workspace` reads, searches, atomically edits, and runs bounded subprocesses |
| Output parsing | `OpenAICompatibleClient._parse_response` validates every required response shape |
| Termination | Final text, max steps, two empty replies, or three consecutive identical calls |
| Error handling | Structured tool errors, HTTP retry/backoff, timeouts, truncation, and secret redaction |

## Tools

- `list_files`: bounded traversal with noisy cache/build directories omitted.
- `read_file`: UTF-8 text with stable line numbers and binary rejection.
- `search_text`: literal, glob-aware repository search.
- `write_file`: atomic create/replace inside the workspace.
- `replace_text`: exact, count-checked atomic edit that fails safely on stale context.
- `run_command`: subprocess in the workspace with timeout, bounded output, stripped
  credential variables, and a blocklist for obviously destructive/external commands.

## Context management

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
| Stale exact edit | Reject; model must re-read instead of corrupting the file |
| Empty model reply | One corrective reprompt, then stop |
| Repeating loop | Stop on the third consecutive identical call |
| Runaway task | Hard `max_steps` limit |

## Security boundary and trade-offs

Path tools resolve symlinks and reject locations outside the selected workspace. API keys
are sent only in the model HTTP header, never logged, and credential-shaped environment
variables are removed from subprocesses. `safe`, `ask`, and read-only `never` approval
modes make the write boundary visible.

`run_command` is intentionally capable and is not an OS sandbox. For hostile repositories,
run Rivet in a disposable container or VM and use `--approval ask`. The project favors a
transparent 600-line core over provider-specific features. Chat Completions is widely
supported by compatible gateways; a second protocol can be added behind the `ModelClient`
interface without changing the agent loop.

