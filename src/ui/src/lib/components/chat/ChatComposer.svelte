<script lang="ts">
	import KnowledgeSearch from './KnowledgeSearch.svelte';
	import type { KnowledgeHit } from '$lib/types';

	/**
	 * Message composer. Steering is a normal message: when an execution is
	 * active, sending percolates to it and the acknowledgement renders
	 * inline in the transcript. Knowledge-search results are injected as
	 * inspectable context.
	 */
	let {
		onSubmit,
		onUpload,
		strategies = [],
		activeExecutionId = null,
		busy = false,
		compact = false
	}: {
		onSubmit: (input: { body: string; attachmentIds: string[]; steeringIntent: string | null }) => void;
		onUpload?: (file: File) => void;
		strategies?: { id: string; name: string }[];
		activeExecutionId?: string | null;
		busy?: boolean;
		compact?: boolean;
	} = $props();

	let body = $state('');
	let attachedStrategy = $state<string | null>(null);
	let attachments = $state<{ id: string; name: string }[]>([]);
	let knowledgeOpen = $state(false);

	const canSend = $derived(body.trim().length > 0 && !busy);

	function attachContext(event: Event): void {
		const select = event.currentTarget as HTMLSelectElement;
		attachedStrategy = select.value || null;
	}

	function upload(event: Event): void {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		if (file) {
			const id = `att-${Math.random().toString(16).slice(2, 10)}`;
			attachments = [...attachments, { id, name: file.name }];
			onUpload?.(file);
		}
		input.value = '';
	}

	function send(): void {
		if (!canSend) return;
		const contextPrefix = attachedStrategy ? `[context: strategy ${attachedStrategy}] ` : '';
		const knowledgePrefix = injected.length
			? injected.map((hit) => `\n[context: knowledge ${hit.path}@${hit.revision}] ${hit.excerpt}`).join('')
			: '';
		const steering = activeExecutionId && isSteering(body) ? body.trim() : null;
		onSubmit({
			body: `${contextPrefix}${body.trim()}${knowledgePrefix}`,
			attachmentIds: attachments.map((a) => a.id),
			steeringIntent: steering
		});
		body = '';
		attachedStrategy = null;
		attachments = [];
		injected = [];
	}

	function isSteering(text: string): boolean {
		// A message steers when an execution is active; the API decides the
		// percolation. The composer only marks intent for the ack display.
		return /\b(steer|focus|restrict|skip|instead|priorit)/i.test(text);
	}

	let injected = $state<KnowledgeHit[]>([]);

	function inject(hit: KnowledgeHit): void {
		injected = [...injected, hit];
		knowledgeOpen = false;
	}
</script>

<div class="border-t border-white/5 bg-surface-900/70 p-3" data-testid="chat-composer">
	{#if attachments.length || attachedStrategy || injected.length}
		<div class="mb-2 flex flex-wrap gap-1.5">
			{#if attachedStrategy}
				<span
					class="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 font-mono text-[10px] text-accent"
				>
					strategy: {attachedStrategy}
				</span>
			{/if}
			{#each attachments as attachment (attachment.id)}
				<span
					class="rounded-full border border-warn/40 bg-warn/10 px-2 py-0.5 font-mono text-[10px] text-warn"
					title="Stored as workspace-scoped untrusted evidence (content hash + provenance)"
				>
					↑ {attachment.name} · untrusted
				</span>
			{/each}
			{#each injected as hit (hit.path)}
				<span
					class="max-w-72 truncate rounded-full border border-white/10 bg-surface-850 px-2 py-0.5 font-mono text-[10px] text-slate-400"
					title="{hit.path}@{hit.revision}: {hit.excerpt}"
				>
					❡ {hit.title} @{hit.revision}
				</span>
			{/each}
		</div>
	{/if}

	<div class="flex items-end gap-2">
		<textarea
			class="max-h-32 min-h-10 flex-1 resize-y rounded-lg border border-surface-600/60 bg-surface-950/70 px-3 py-2 text-sm text-slate-200 outline-none placeholder:text-slate-600 focus:border-accent/50"
			placeholder={activeExecutionId
				? 'Message the workspace agent — an execution is active; messages percolate as steering…'
				: 'Message the workspace agent…'}
			bind:value={body}
			onkeydown={(e) => {
				if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send();
			}}
		></textarea>
		<button
			type="button"
			class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-2 text-sm font-semibold text-accent transition hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
			disabled={!canSend}
			onclick={send}
			data-testid="composer-send"
		>
			Send
		</button>
	</div>

	<div class="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
		<label class="flex items-center gap-1 text-slate-500">
			<span class="hidden sm:inline">Attach</span>
			<select
				class="rounded border border-surface-600/60 bg-surface-950/70 px-1.5 py-1 text-[11px] text-slate-300 outline-none"
				onchange={attachContext}
				value={attachedStrategy ?? ''}
				aria-label="Attach strategy context"
			>
				<option value="">strategy context…</option>
				{#each strategies as strategy (strategy.id)}
					<option value={strategy.id}>{strategy.name}</option>
				{/each}
			</select>
		</label>
		<label
			class="cursor-pointer rounded border border-surface-600/60 px-1.5 py-1 text-[11px] text-slate-400 hover:text-slate-200"
		>
			↑ upload evidence
			<input type="file" class="hidden" onchange={upload} />
		</label>
		<button
			type="button"
			class="rounded border border-surface-600/60 px-1.5 py-1 text-[11px] text-slate-400 hover:text-slate-200
				{knowledgeOpen ? 'border-accent/40 text-accent' : ''}"
			onclick={() => (knowledgeOpen = !knowledgeOpen)}
		>
			❡ knowledge search
		</button>
		{#if activeExecutionId}
			<span class="ml-auto flex items-center gap-1 font-mono text-[10px] text-warn/80">
				<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-warn" aria-hidden="true"></span>
				execution active · messages percolate (durable ack)
			</span>
		{/if}
	</div>

	{#if knowledgeOpen && !compact}
		<div class="mt-2">
			<KnowledgeSearch onInject={inject} />
		</div>
	{/if}
</div>
