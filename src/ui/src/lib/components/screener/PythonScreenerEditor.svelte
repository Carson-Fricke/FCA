<script lang="ts">
	/**
	 * Custom screener editor: one `def screen(ctx)` Python filter in a
	 * monospace textarea with client-side boundary feedback (bracket
	 * balance, single screen function, import allowlist, no aliases or
	 * dunders) and inline rendering of server-side lint rejections.
	 */
	import { SCREEN_TEMPLATE, lintScreenScript } from './screenScript';

	let { serverError = null }: { serverError?: string | null } = $props();

	let source = $state(SCREEN_TEMPLATE);

	const issues = $derived(lintScreenScript(source));
	const errors = $derived(issues.filter((issue) => issue.severity === 'error'));
	const warnings = $derived(issues.filter((issue) => issue.severity === 'warning'));
</script>

<section class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="script-editor">
	<h3 class="text-sm font-semibold text-slate-800">
		Custom screen script
		<span class="font-mono text-[10px] text-slate-500">def screen(ctx)</span>
	</h3>
	<p class="mt-1 text-[11px] text-slate-500">
		One <code class="font-mono">def screen(ctx) → list[dict]</code> function; imports restricted to
		<code class="font-mono">math</code> and <code class="font-mono">statistics</code>; no aliases,
		no dunders, no other top-level statements. Execution runs through the fail-closed sandbox
		boundary.
	</p>
	<textarea
		class="mt-2 h-64 w-full resize-y rounded-lg border border-surface-600/60 bg-surface-950 p-3 font-mono text-[11px] leading-5 text-slate-800 outline-none focus:border-accent/50"
		spellcheck="false"
		aria-label="Custom screen script source"
		bind:value={source}
		data-testid="script-source"
	></textarea>
	{#if errors.length}
		<ul class="mt-2 space-y-0.5" data-testid="script-errors">
			{#each errors as issue (`${issue.line}:${issue.message}`)}
				<li class="font-mono text-[10px] text-loss">
					{issue.line !== null ? `line ${issue.line}: ` : ''}{issue.message}
				</li>
			{/each}
		</ul>
	{:else if warnings.length}
		<ul class="mt-2 space-y-0.5" data-testid="script-warnings">
			{#each warnings as issue (`${issue.line}:${issue.message}`)}
				<li class="font-mono text-[10px] text-warn">
					{issue.line !== null ? `line ${issue.line}: ` : ''}{issue.message}
				</li>
			{/each}
		</ul>
	{:else}
		<p class="mt-2 font-mono text-[10px] text-accent" data-testid="script-clean">boundary check clean</p>
	{/if}
	{#if serverError}
		<p
			class="mt-2 rounded-lg border border-loss/40 bg-loss/10 px-2 py-1.5 font-mono text-[10px] text-loss"
			data-testid="script-server-error"
		>
			{serverError}
		</p>
	{/if}
</section>
