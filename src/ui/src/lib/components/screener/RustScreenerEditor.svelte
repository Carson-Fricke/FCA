<script lang="ts">
	/**
	 * Rust screening editor: the authored `export_screening_program!` source,
	 * monospaced and bound to the panel. The only client-side pre-flight is a
	 * light scan (balanced brackets, `fn screen`, the export macro); the
	 * harness JIT compiler is authoritative and its diagnostics arrive with
	 * the run response.
	 */
	import { lintScreenerSource } from './screenerSource';

	let {
		source = $bindable(),
		sourceDirty = false,
		serverError = null,
		onEdited,
		onGenerate
	}: {
		source: string;
		sourceDirty?: boolean;
		serverError?: string | null;
		onEdited: () => void;
		onGenerate: () => void;
	} = $props();

	const issues = $derived(lintScreenerSource(source));
	const errors = $derived(issues.filter((issue) => issue.severity === 'error'));
</script>

<section class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="rust-screener-editor">
	<div class="flex flex-wrap items-center gap-2">
		<h3 class="text-sm font-semibold text-slate-800">Authored Rust program</h3>
		{#if sourceDirty}
			<span
				class="rounded-full border border-warn/40 bg-warn/10 px-2 py-0.5 text-[10px] text-warn"
				data-testid="script-manual"
			>
				manual edits
			</span>
		{:else}
			<span
				class="rounded-full border border-surface-600 px-2 py-0.5 text-[10px] text-slate-500"
				data-testid="script-generated"
			>
				generated from filters
			</span>
		{/if}
		<button
			type="button"
			class="ml-auto rounded-lg border border-surface-600 px-2.5 py-1 text-xs text-slate-500 hover:border-white/20 hover:text-slate-800"
			onclick={onGenerate}
			data-testid="script-regenerate"
		>
			Regenerate from filters
		</button>
	</div>

	<textarea
		class="mt-3 min-h-72 w-full resize-y rounded-lg border border-surface-600 bg-surface-950 p-3 font-mono text-xs leading-relaxed text-slate-700 outline-none focus:border-accent/50"
		spellcheck="false"
		autocomplete="off"
		autocapitalize="off"
		bind:value={source}
		oninput={onEdited}
		aria-label="Screening program source"
		data-testid="script-source"
	></textarea>

	{#if errors.length}
		<ul
			class="mt-2 space-y-1 rounded-lg border border-loss/40 bg-loss/10 px-3 py-2"
			data-testid="script-errors"
		>
			{#each errors as issue, index (`${index}:${issue.message}`)}
				<li class="font-mono text-[10px] text-loss">
					{#if issue.line !== null}line {issue.line}: {/if}{issue.message}
				</li>
			{/each}
		</ul>
	{:else}
		<p class="mt-2 font-mono text-[10px] text-slate-500" data-testid="script-clean">
			pre-flight clean — the harness compiler is authoritative
		</p>
	{/if}

	{#if serverError}
		<p
			class="mt-2 rounded-lg border border-loss/40 bg-loss/10 px-3 py-2 font-mono text-[10px] text-loss"
			data-testid="script-server-error"
		>
			{serverError}
		</p>
	{/if}
</section>
