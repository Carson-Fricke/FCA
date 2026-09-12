<script lang="ts">
	import type { ScopeSegment } from '$lib/scope.svelte';

	/**
	 * In-pane navigation stack for entering sub-agents:
	 * conversation → workflow → orchestrator → sub-agent → …
	 *
	 * Renders a breadcrumb with a back control; the host supplies the current
	 * node's content through the default snippet. Hosted by the conversation
	 * pane and by the standalone `/w/{workspace}/workflows/{execution_id}`
	 * route, so entering/back behaves identically in both.
	 */
	let {
		segments = [],
		onBack = () => {},
		onNavigate = () => {},
		title = 'Scope',
		children
	}: {
		segments?: ScopeSegment[];
		onBack?: () => void;
		onNavigate?: (index: number) => void;
		title?: string;
		children?: import('svelte').Snippet;
	} = $props();

	const activeIndex = $derived(Math.max(0, segments.length - 1));
</script>

<section
	class="flex min-h-0 flex-col rounded-xl border border-surface-700 bg-surface-900"
	data-testid="scope-stack"
	data-depth={segments.length}
>
	<header class="flex items-center gap-2 border-b border-surface-700 px-3 py-2">
		<button
			type="button"
			class="rounded-md border border-surface-600 px-2 py-0.5 text-xs text-slate-600 transition hover:border-accent/40 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
			disabled={segments.length <= 1}
			onclick={onBack}
			data-testid="scope-back"
			title="Back one scope level"
		>
			← Back
		</button>
		<span class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">{title}</span>
		<nav class="ml-auto min-w-0 overflow-x-auto" aria-label="Scope breadcrumb" data-testid="scope-breadcrumb">
			<ol class="flex items-center gap-1 whitespace-nowrap">
				{#each segments as segment, index (segment.kind + ':' + segment.id)}
					<li class="flex items-center gap-1">
						{#if index > 0}
							<span class="text-slate-600" aria-hidden="true">/</span>
						{/if}
						<button
							type="button"
							class="max-w-40 truncate rounded px-1.5 py-0.5 font-mono text-[10px] transition {index ===
							activeIndex
								? 'bg-accent/10 text-accent'
								: 'text-slate-500 hover:text-accent'}"
							onclick={() => onNavigate(index)}
							aria-current={index === activeIndex ? 'page' : undefined}
							data-testid="scope-crumb"
							data-scope-index={index}
							title={segment.label}
						>
							{segment.label}
						</button>
					</li>
				{/each}
			</ol>
		</nav>
	</header>

	<div class="min-h-0 flex-1 overflow-y-auto p-3">
		{@render children?.()}
	</div>
</section>
