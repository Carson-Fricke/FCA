<script lang="ts">
	import MarkdownBody from './MarkdownBody.svelte';
	import ThinkingAccordion from './ThinkingAccordion.svelte';
	import ToolResultCard from './ToolResultCard.svelte';
	import FalsificationReportCard from './FalsificationReportCard.svelte';
	import TypingDots from '$lib/components/common/TypingDots.svelte';
	import type { ScopeEntry } from '$lib/transcript';
	import type { ExecutionEvent, SubAgentNode } from '$lib/types';

	/**
	 * One scope's unified, chronological transcript inside the chat pane.
	 *
	 * Unlike the root `ChatTranscript`, this surface renders a sub-agent's
	 * conversation: the messages it received, its thinking/replies, its tool
	 * renderings, and clickable child sub-agents, all on one timeline. Child
	 * cards drill in-place (the host pushes a scope segment); the composer lives
	 * in the host so every scope keeps the same send controls.
	 */
	let {
		entries,
		workspaceId,
		busy = false,
		readOnly = false,
		pending = false,
		onEnterSubAgent = () => {}
	}: {
		entries: ScopeEntry[];
		workspaceId: string;
		busy?: boolean;
		readOnly?: boolean;
		/** The agent is mid-turn; show a typing indicator. */
		pending?: boolean;
		onEnterSubAgent?: (node: SubAgentNode) => void;
	} = $props();

	const isUser = (role: string) => role === 'user';

	const STATUS_TONE: Record<string, string> = {
		spawned: 'border-accent/40 bg-accent/10 text-accent',
		running: 'border-accent/40 bg-accent/10 text-accent',
		progress: 'border-accent/40 bg-accent/10 text-accent',
		needs_input: 'border-warn/40 bg-warn/10 text-warn',
		completed: 'border-profit/40 bg-profit/10 text-profit',
		failed: 'border-loss/40 bg-loss/10 text-loss'
	};

	const EVENT_LABEL: Record<string, string> = {
		'sub_agent.spawned': 'sub-agent started',
		'sub_agent.completed': 'sub-agent completed',
		'sub_agent.failed': 'sub-agent failed',
		'workflow.launched': 'workflow launched',
		'workflow.started': 'workflow started',
		'workflow.completed': 'workflow completed',
		'workflow.failed': 'workflow failed',
		'tool.started': 'tool started'
	};

	function eventLabel(event: ExecutionEvent): string {
		return EVENT_LABEL[event.eventType] ?? event.eventType;
	}
</script>

<div class="space-y-3" data-testid="scope-transcript">
	{#each entries as entry (entry.id)}
		{#if entry.kind === 'message'}
			{#if entry.message.role === 'tool'}
				<div
					class="flex justify-start"
					data-testid="chat-message"
					data-role="tool"
					data-message-id={entry.message.message_id}
				>
					<div class="w-full">
						{#if entry.message.tool_result}
							<ToolResultCard result={entry.message.tool_result} {workspaceId} />
						{:else}
							<p class="text-sm whitespace-pre-wrap text-slate-800">{entry.message.body}</p>
						{/if}
					</div>
				</div>
			{:else}
				<article
					class="flex {isUser(entry.message.role) ? 'justify-end' : 'justify-start'}"
					data-testid="chat-message"
					data-role={entry.message.role}
					data-message-id={entry.message.message_id}
				>
					<div
						class="min-w-0 {isUser(entry.message.role)
							? 'max-w-[85%] rounded-xl border border-surface-700 bg-profit/10 p-3'
							: 'w-full p-1'}"
					>
						<header class="mb-1 flex items-center gap-2 text-[10px] tracking-wider">
							<span
								class="uppercase {isUser(entry.message.role)
									? 'font-semibold text-accent'
									: 'text-slate-500'}"
							>
								{entry.message.role}
							</span>
							<span class="font-mono text-slate-500">{entry.message.actor_id}</span>
							<time class="font-mono text-slate-500">{entry.message.created_at.slice(11, 19)}</time>
						</header>
						{#if entry.message.reasoning}
							<ThinkingAccordion
								text={entry.message.reasoning}
								testId="thinking-{entry.message.message_id}"
							/>
						{/if}
						{#if isUser(entry.message.role)}
							<p class="text-sm whitespace-pre-wrap text-slate-800">{entry.message.body}</p>
						{:else}
							<MarkdownBody text={entry.message.body} />
						{/if}
					</div>
				</article>
			{/if}
		{:else if entry.kind === 'falsification'}
			<FalsificationReportCard data={entry.data} {workspaceId} />
		{:else if entry.kind === 'tool'}
			<div class="flex justify-start" data-testid="scope-tool">
				<div class="w-full">
					<ToolResultCard result={entry.result} {workspaceId} />
				</div>
			</div>
		{:else if entry.kind === 'thinking'}
			<ThinkingAccordion
				text={entry.text}
				testId="scope-thinking-{entry.id}"
				initiallyOpen={true}
			/>
		{:else if entry.kind === 'spawn'}
			<section
				class="rounded-xl border border-surface-700 bg-surface-950/40 p-3"
				data-testid="scope-spawn"
			>
				<header class="flex flex-wrap items-center gap-2">
					<span class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
						initial context
					</span>
					<span class="text-sm font-semibold text-slate-800">{entry.spawn.role}</span>
					{#if entry.spawn.phase}
						<span class="font-mono text-[10px] text-slate-500">{entry.spawn.phase}</span>
					{/if}
				</header>
			</section>
		{:else if entry.kind === 'assistant'}
			<article
				class="flex justify-start"
				data-testid="scope-assistant"
				data-role="assistant"
				data-needs-input={entry.needsInput ? 'true' : 'false'}
			>
				<div
					class="w-full p-1 {entry.needsInput
						? 'rounded-xl border border-warn/40 bg-warn/5 p-3'
						: ''}"
				>
					<header class="mb-1 flex items-center gap-2 text-[10px] tracking-wider">
						<span class="text-slate-500 uppercase">
							{entry.needsInput ? 'question' : 'assistant'}
						</span>
						<span class="font-mono text-slate-500">{entry.actorId}</span>
						{#if entry.needsInput}
							<span
								class="rounded-full border border-warn/40 bg-warn/10 px-2 py-0.5 font-mono text-[10px] text-warn"
								data-testid="scope-needs-input"
							>
								needs input
							</span>
						{/if}
					</header>
					<MarkdownBody text={entry.text} />
				</div>
			</article>
		{:else if entry.kind === 'sub-agent'}
			<button
				type="button"
				class="flex w-full items-start gap-3 rounded-xl border border-surface-700 bg-surface-900 p-3 text-left transition hover:border-accent/40 hover:bg-surface-800/50"
				onclick={() => onEnterSubAgent(entry.node)}
				data-testid="scope-sub-agent"
				data-sub-agent={entry.node.subAgentId}
				title="Open this sub-agent"
			>
				<div class="min-w-0 flex-1">
					<div class="flex flex-wrap items-center gap-2">
						<span class="truncate text-sm font-semibold text-slate-800">
							{entry.node.role || entry.node.subAgentId}
						</span>
						{#if entry.node.phase}
							<span class="font-mono text-[10px] text-slate-500">{entry.node.phase}</span>
						{/if}
						<span
							class="rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase {STATUS_TONE[
								entry.node.status
							] ?? 'border-surface-600 text-slate-500'}"
						>
							{entry.node.status}
						</span>
					</div>
					<p class="mt-0.5 font-mono text-[10px] text-slate-500">{entry.node.subAgentId}</p>
				</div>
				<span class="mt-0.5 text-xs text-slate-500" aria-hidden="true">›</span>
			</button>
		{:else}
			<div
				class="flex items-center gap-2 px-1 font-mono text-[10px] text-slate-500"
				data-testid="scope-event"
				data-event={entry.event.eventType}
			>
				<span class="h-1 w-1 rounded-full bg-surface-600" aria-hidden="true"></span>
				{eventLabel(entry.event)}
				<time>{entry.at.slice(11, 19)}</time>
			</div>
		{/if}
	{/each}

	{#if !entries.length}
		<p class="px-1 py-4 text-center text-xs text-slate-500" data-testid="scope-empty">
			{readOnly ? 'No scoped activity recorded.' : 'No scoped activity yet.'}
		</p>
	{/if}

	{#if pending}
		<div class="flex items-center gap-2 px-1 py-1" data-testid="scope-pending">
			<TypingDots />
		</div>
	{/if}
</div>
