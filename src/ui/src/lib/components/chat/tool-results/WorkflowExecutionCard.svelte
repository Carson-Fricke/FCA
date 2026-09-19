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
		readString(structured, 'executionId') ?? readString(structured, 'execution_id')
	);

	const live = $derived.by<WorkflowExecutionRecord | null>(() => {
		const view = liveView?.();
		if (!view?.execution || !executionId) return null;
		return view.execution.execution_id === executionId ? view.execution : null;
	});

	const execution = $derived.by<WorkflowExecutionRecord | null>(() => {
		if (live) return live;
		if (!executionId) return null;
		return {
			execution_id: executionId,
			chat_id: '',
			trigger_message_id: '',
			workflow_id:
				readString(structured, 'workflowId') ??
				readString(structured, 'workflow_id') ??
				'workflow-execution',
			workflow_version:
				readString(structured, 'version') ??
				readString(structured, 'workflowVersion') ??
				'',
			requested_by: '',
			state: readString(structured, 'state') ?? 'running',
			phase: readString(structured, 'phase'),
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
		live ? (liveView?.()?.phases ?? persistedPhases) : persistedPhases
	);

	const attentionPhases = $derived(
		live ? (liveView?.()?.attentionPhases ?? []) : []
	);

	const status = $derived(
		live?.state ??
			readString(structured, 'state') ??
			readString(structured, 'status') ??
			readString(structured, 'phase') ??
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

	const onEnterPhase = $derived(liveView?.()?.onEnterPhase ?? (() => {}));
</script>

<WorkflowCard
	{execution}
	{phases}
	{status}
	{summary}
	{attentionPhases}
	{onEnterPhase}
/>
