"""Conservative MCP error receipts from passive, local execution evidence.

This module never dispatches, probes a guest, clears a journal, or retries work.
A missing journal after a handler started is not proof that no action occurred.
"""
from __future__ import annotations

import json
import subprocess

# These tools cannot dispatch native Prism/PPT actions. Preparing a request may
# write local files, so it deliberately does not receive observation retries.
OBSERVATION_TOOLS = frozenset({
    'prism_env', 'prism_windows_catalog', 'prism_windows_scope',
    'prism_windows_blueprint', 'prism_list_templates', 'prism_list_palettes',
    'prism_list_template_catalog', 'prism_match_template', 'prism_inspect_template',
})
PENDING = frozenset({'active', 'unknown', 'native_error', 'interactive'})


def snapshot(bridge):
    """Return bounded queue identities only; never refresh the native worker."""
    result = {}
    readers = {'native': lambda: bridge.execution_status(passive=True),
               'ppt': bridge.windows_prism_ppt.execution_status}
    for queue, read in readers.items():
        try:
            value = read()
            result[queue] = {key: value.get(key) for key in
                             ('state', 'operation_id', 'blocked', 'late_completion_observed')}
        except Exception:
            result[queue] = {'state': 'unavailable', 'operation_id': None, 'blocked': True}
    return result


def native_failure(content, tool):
    """Recognize native failure flags, not the preview's publication disclaimer."""
    if not content or content[0].get('type') != 'text':
        return None
    try:
        value = json.loads(content[0]['text'])
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if (value.get('prism_run_ok') is False or value.get('render_ok') is False
            or (tool == 'prism_export_project' and value.get('ok') is False)):
        return value
    return None


def make_receipt(error, *, tool, phase, before=None, after=None,
                 invalid_arguments=False, runtime_status=None):
    """Describe evidence, never infer safe replay from an exception class."""
    message = str(error)[:2000]
    result = dict(runtime_status or {})
    result.update(error_code='TOOL_FAILED', message=message, operation_id=None,
                  execution_state='unknown', retryable=False,
                  next_action='Inspect prism_env with runtime_only=true and the original outputs; do not replay the action.',
                  operation_scope=None)
    if phase != 'execute' or invalid_arguments:
        result['execution_state'] = 'not_started'
        if runtime_status is not None:
            result.update(error_code='RUNTIME_RESTART_REQUIRED',
                          next_action='Reconnect the Prism MCP server, then check prism_env(runtime_only=true). Do not restart Prism to reload MCP code.')
        elif phase == 'environment':
            result.update(error_code='ENVIRONMENT_NOT_READY',
                          next_action='Check prism_env(runtime_only=true), configure the required Windows environment, and verify readiness before a new request.')
        else:
            result.update(error_code='INVALID_ARGUMENTS' if invalid_arguments or phase == 'arguments' else 'TOOL_NOT_STARTED',
                          next_action='Correct the request or dependency reported in message before submitting a new request.')
        return result

    if tool in OBSERVATION_TOOLS:
        transient = isinstance(error, (TimeoutError, ConnectionError, subprocess.SubprocessError))
        result.update(error_code='OBSERVATION_UNAVAILABLE' if transient else 'TOOL_FAILED',
                      execution_state='not_started', retryable=transient,
                      next_action=('Retry this read-only query once after its dependency recovers; do not replay native actions.' if transient
                                   else 'Inspect the reported read-only dependency or request and correct it before retrying.'))
        return result

    before, after = before or {}, after or {}
    if any(v.get('state') in ('invalid_journal', 'unavailable') for v in after.values()):
        result.update(error_code='EXECUTION_STATE_UNAVAILABLE',
                      next_action='Inspect prism_env(runtime_only=true) and preserve the journals. Do not delete records or replay actions.')
        return result

    # Journals are shared by all Bridge instances. IDs below identify observed
    # queue records, not a claimed causal relationship to this MCP request.
    candidates = []
    for queue, value in after.items():
        ident = value.get('operation_id')
        if not ident:
            continue
        previous = before.get(queue, {})
        changed = any(value.get(k) != previous.get(k) for k in ('operation_id', 'state', 'late_completion_observed'))
        if changed or value.get('blocked') or ident in message:
            candidates.append(dict(value, queue=queue))
    pending = [v for v in candidates if v.get('state') in PENDING and not v.get('late_completion_observed')]
    relevant = pending or candidates
    if len(relevant) == 1:
        observed = relevant[0]
        result.update(operation_id=observed['operation_id'], operation_scope='observed_shared_queue',
                      operation_queue=observed['queue'])
        state = observed.get('state')
        if observed.get('late_completion_observed') or state == 'completed_late':
            result.update(error_code='EXECUTION_COMPLETED_LATE', execution_state='completed_late',
                          next_action='Inspect the original outputs. Reconcile this operation_id with prism_recover_execution if still pending; do not replay the action.')
        elif state == 'completed':
            result.update(error_code='RESULT_VERIFICATION_FAILED', execution_state='completed',
                          next_action='Inspect the original outputs and the reported post-processing failure. Native completion does not verify the final result; do not replay.')
        elif state in ('not_started', 'cancelled_before_dispatch'):
            result.update(error_code='EXECUTION_NOT_STARTED', execution_state='not_started',
                          next_action='Inspect this operation_id and correct its pre-dispatch failure before preparing a new request.')
        else:
            result.update(error_code='EXECUTION_OUTCOME_UNKNOWN',
                          next_action='Query prism_env(runtime_only=true), then reconcile this exact operation_id with prism_recover_execution. Preserve original outputs and do not replay.')
    else:
        result.update(error_code='EXECUTION_OUTCOME_UNKNOWN',
                      next_action='Query prism_env(runtime_only=true) and reconcile the native/PPT records and original outputs. No single operation is established; do not replay.')
    return result
