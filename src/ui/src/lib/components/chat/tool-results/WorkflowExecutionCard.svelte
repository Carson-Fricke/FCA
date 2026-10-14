<script lang="ts">
	import WorkflowCard from '../WorkflowCard.svelte';
	import { getWorkflowLiveView } from '../workflow-live-view';
	import type { ToolResult, WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';
	import { isRecord, readArray, readString } from './safe';

	/**
	 * Renderer adapter for the `workflow_execution` datum.
	 *
	 * The datum's `structured` carries the execution handle; the adapter maps it
	 * onto the canonical `WorkflowCard`, so the root transcript and every node
	 * scope render the same card through the one registry route.  When the root
	 * transcript provides the live workflow view for the same execution, the
	 * card upgrades to the live execution, phases, attention state, and phase
	 * navigation instead of deriving them from the persisted datum alone.
	 */
	let { result }: { result: ToolResult; workspaceId: string } = $props();

	const liveView = getWorkflowLiveView();

	const structured = $derived(isRecord(result.structured) ? result.structured : {});

	const executionId = $derived(
		readString(structured, 'executionId')
	);
	const view = $derived(executionId ? liveView?.(executionId) : null);

	const live = $derived.by<WorkflowExecutionRecord | null>(() => {
		if (!view?.execution || !executionId) return null;
		return view.execution.execution_id === executionId ? view.execution : null;
	});

	const execution = $derived.by<WorkflowExecutionRecord | null>(() => {
		if (live) return live;
		if (!executionId) return null;
        const state = readString(structured, 'state');
        if (state !== 'queued' && state !== 'running' && state !== 'stopped' && state !== 'cancelled' && state !== 'completed' && state !== 'failed') return null;
		return {
			execution_id: executionId,
			chat_id: '',
			trigger_message_id: '',
			workflow_id:
				readString(structured, 'workflowId') ??
				'',
			workflow_version:
				readString(structured, 'version') ??
				'',
			requested_by: '',
			state,
			active_phases: readArray<string>(structured, 'activePhases'),
			intent_epoch: 0,
			steering_message_ids: [],
			created_at: ''
		};
	});

	const persistedPhases = $derived.by<WorkflowPhase[]>(() => {
		const parsed: WorkflowPhase[] = [];
		for (const item of readArray<unknown>(structured, 'phases')) {
			if (typeof item === 'string') {
				parsed.push({ name: item, status: 'pending' });
				continue;
			}
			if (!isRecord(item)) continue;
			const name = readString(item, 'name');
			const status = readString(item, 'status');
			if (!name) continue;
			parsed.push({
				name,
				status: status === 'done' || status === 'active' ? status : 'pending'
			});
		}
		return parsed;
	});

	const phases = $derived(
		live ? (view?.phases ?? persistedPhases) : persistedPhases
	);

	const attentionPhases = $derived(
		live ? (view?.attentionPhases ?? []) : []
	);

	const status = $derived(
		live?.state ??
			readString(structured, 'state') ??
			'running'
	);

	const summary = $derived.by(() => {
		if (live) {
			return (
				(live.phase ? `Current phase ${live.phase}` : undefined) ??
				readString(structured, 'title') ??
				result.summary ??
				'Workflow launched'
			);
		}
		return result.summary || (readString(structured, 'summary') ?? '');
	});

	const onEnterPhase = $derived(view?.onEnterPhase ?? (() => {}));
</script>

<WorkflowCard
	{execution}
	{phases}
	{status}
	{summary}
	jobIds={view?.jobIds ?? []}
	{attentionPhases}
	{onEnterPhase}
/>
