<script lang="ts">
	import type { ScopeSegment } from '$lib/scope.svelte';

	/**
	 * In-pane navigation stack for entering sub-agents:
	 * conversation → workflow → orchestrator → sub-agent → …
	 *
	 * Renders a breadcrumb with a back control; the host supplies the current
	 * node's content through the default snippet. Hosted by the shared chat
	 * surface, so entering/back behaves identically in the copilot pane and the
	 * full conversations page.
	 */
	let {
		segments = [],
		onBack = () => {},
		onNavigate = () => {},
		title = 'Scope',
		showHeader = true,
		controls,
		children
	}: {
		segments?: ScopeSegment[];
		onBack?: () => void;
		onNavigate?: (index: number) => void;
		title?: string;
		showHeader?: boolean;
		controls?: import('svelte').Snippet;
		children?: import('svelte').Snippet;
	} = $props();

	const activeIndex = $derived(Math.max(0, segments.length - 1));
</script>

<section
	class="flex min-h-0 flex-1 flex-col"
	data-testid="scope-stack"
	data-depth={segments.length}
>
	{#if showHeader}
	<header class="scope-header">
		<span class="scope-kind">{title}</span>
		<div class="scope-navigation">
		<button
			type="button"
			class="scope-back"
			disabled={segments.length <= 1}
			onclick={onBack}
			data-testid="scope-back"
			title="Back one scope level"
			aria-label="Back one scope level"
		>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M19 12H5m6-6-6 6 6 6" stroke-linecap="round" stroke-linejoin="round"/></svg>
		</button>
		<nav class="scope-breadcrumb" aria-label="Scope breadcrumb" data-testid="scope-breadcrumb">
			<ol>
				{#each segments as segment, index (segment.kind + ':' + segment.id)}
					<li>
						{#if index > 0}
							<span class="scope-separator" aria-hidden="true">›</span>
						{/if}
						<button
							type="button"
							class:current={index === activeIndex}
							onclick={() => onNavigate(index)}
							aria-current={index === activeIndex ? 'page' : undefined}
							data-testid="scope-crumb"
							data-scope-index={index}
							title={segment.label}
						>
							{segment.kind === 'conversation' ? 'Conversation' : segment.label.replace(/_/g, ' ')}
						</button>
					</li>
				{/each}
			</ol>
		</nav>
		{@render controls?.()}
		</div>
	</header>
	{/if}

	{@render children?.()}
</section>

<style>
	.scope-header{padding:8px 12px;border-bottom:1px solid var(--color-surface-700)}
	.scope-kind{display:block;margin-left:39px;margin-bottom:2px;font-size:10px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--color-slate-400)}
	.scope-navigation{display:flex;align-items:flex-start;gap:9px;min-width:0}
	.scope-back{display:flex;align-items:center;justify-content:center;flex-shrink:0;width:30px;height:30px;border:1px solid var(--color-surface-600);border-radius:5px;color:var(--color-slate-300);cursor:pointer}.scope-back:disabled{opacity:.4;cursor:default}.scope-back svg{width:14px;height:14px}
	.scope-breadcrumb{flex:1;min-width:0;padding-top:2px;font-size:12px;line-height:1.7}.scope-breadcrumb ol{display:flex;align-items:center;flex-wrap:wrap;gap:3px 0;margin:0;padding:0;list-style:none}.scope-breadcrumb li{display:flex;align-items:baseline;gap:7px;max-width:100%;padding-right:7px;min-width:0}
	.scope-breadcrumb button{padding:1px 3px;border-radius:4px;text-align:left;overflow-wrap:anywhere;color:var(--color-slate-200);cursor:pointer}.scope-breadcrumb button:hover{text-decoration:underline;text-underline-offset:3px;color:var(--color-accent)}.scope-breadcrumb button.current{color:var(--color-accent);background:color-mix(in srgb,var(--color-accent) 9%,var(--color-surface-900));padding:1px 6px}.scope-separator{color:var(--color-slate-400)}
	@media(pointer:coarse){.scope-back,.scope-breadcrumb button{min-height:44px}}
</style>
