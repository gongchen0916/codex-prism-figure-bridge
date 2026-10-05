# MCP 0.9.1 error contract

Tool exceptions return a JSON object in the existing first MCP text content block with `isError: true`. No protocol-version change, dynamic tool removal, retry loop, worker restart, or native execution bypass is introduced. Successful content remains unchanged.

## Fields

| Field | Meaning |
| --- | --- |
| `error_code` | Stable category for the failure. |
| `message` | Bounded human-readable diagnostic; do not use it as a replay command. |
| `operation_id` | Observed relevant native/PPT journal ID, or `null` when no single record is established. |
| `execution_state` | `not_started`, `unknown`, `completed_late`, or `completed`. This describes native execution evidence, not scientific or visual correctness. |
| `retryable` | Whether this failed read-only observation can be retried once after recovery. It is never true for an uncertain mutation. |
| `next_action` | Instructions to correct inputs, restore the environment, or reconcile the original operation. |
| `operation_scope` | `observed_shared_queue` when an ID is reported; otherwise `null`. Shared journals may belong to another Bridge instance, so an ID is not a claimed causal link to this request. |
| `operation_queue` | `native` or `ppt` when a single relevant record is identified. |

An argument or environment rejection before a handler runs is `not_started` with no operation ID. A generic exception after a native-capable handler starts stays `unknown` unless a journal provides stronger evidence. No journal is not proof of no execution. Corrupt or unreadable records prohibit retry. A completed native script followed by an output-processing error is not permission to execute it again.

## Categories and recovery

| `error_code` | Required action |
| --- | --- |
| `INVALID_ARGUMENTS` | Correct the request before submitting again. |
| `ENVIRONMENT_NOT_READY` | Inspect `prism_env(runtime_only=true)` and configure/verify the required Windows environment. |
| `RUNTIME_RESTART_REQUIRED` | Reconnect the MCP server and verify loaded/disk identities. Existing runtime diagnostic fields remain present. |
| `OBSERVATION_UNAVAILABLE` | A read-only query failed transiently; retry that query at most once after its dependency recovers. |
| `EXECUTION_OUTCOME_UNKNOWN` | Preserve outputs and query status. Reconcile the exact recorded operation with `prism_recover_execution`; do not replay. |
| `EXECUTION_STATE_UNAVAILABLE` | Inspect and preserve the journals. Do not delete them to clear the fence. |
| `EXECUTION_COMPLETED_LATE` | Inspect original outputs; reconcile the record if still pending. Do not dispatch the action again. |
| `EXECUTION_NOT_STARTED` | A relevant record proves no dispatch; correct the recorded pre-dispatch failure before a new request. |
| `RESULT_VERIFICATION_FAILED` | Native execution completed, but the surrounding request failed. Inspect outputs and repair the reported post-processing step. |
| `TOOL_NOT_STARTED` / `TOOL_FAILED` | Inspect the reported dependency or request; no automatic replay is authorized. |

When both native and PPT records are relevant, an uncertain PPT operation takes precedence over native completion. If no single operation can be selected, `operation_id` stays `null` and the caller must inspect both queues.

## Compatibility

The tool catalog and MCP protocol version remain unchanged. Existing runtime-restart diagnostic fields and numeric `mcp_error_code` values are preserved alongside the new receipt fields.

Some historical tools return JSON summaries instead of raising on native failure. Their existing `isError: false`, `ok`, `prism_run_ok`, and `render_ok` values are preserved; explicit native failure summaries gain the same error fields at the top level. Consumers should check both `isError` and these native outcome fields. An experimental preview's `ok: false` or `safe_for_publication: false` alone does not mean an execution failure.

Error reporting reads local journals passively. It does not contact the VM, clear fences, refresh workers, or retry actions. The existing native dispatch and recovery code remains responsible for concurrency and completion evidence.

## Validation scope

Fault tests exercise environment rejection, invalid arguments, stale runtimes, corrupt journals, missing journals, unknown worker outcomes, multiple queue states, late completion, and unchanged success/preview content. A separate stdio test starts a real child worker, terminates it with a nonzero exit, then verifies that the MCP parent still answers status and tool-list requests.

Native dispatch is simulated in these tests. They do not constitute new Prism drawing or PowerPoint OLE acceptance.
