<script lang="ts">
	import type { EvidenceItem } from '$lib/types';

	/** Evidence badge deep-linking into backtests, dashboards, workspace state. */
	let { evidence }: { evidence: EvidenceItem } = $props();

	const TIER: Record<string, { label: string; cls: string }> = {
		workspace: { label: 'workspace', cls: 'border-accent/40 text-accent' },
		untrusted: { label: 'untrusted', cls: 'border-warn/40 text-warn' },
		external: { label: 'external', cls: 'border-surface-600 text-slate-400' }
	};

	const KIND_ICON: Record<string, string> = {
		backtest: '▦',
		artifact: '⬢',
		knowledge: '❡',
		upload: '↑'
	};
</script>

{#if evidence.link}
	<a
		href={evidence.link}
		class="inline-flex max-w-64 items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10px] transition hover:bg-surface-800
			{TIER[evidence.trust_tier]?.cls ?? 'border-surface-600 text-slate-400'}"
		title="{evidence.reference}{evidence.digest ? ` · ${evidence.digest.slice(0, 16)}…` : ''}"
		data-testid="evidence-badge"
	>
		<span aria-hidden="true">{KIND_ICON[evidence.kind] ?? '•'}</span>
		<span class="truncate">{evidence.title}</span>
		{#if evidence.revision}
			<span class="text-slate-600">@{evidence.revision}</span>
		{/if}
	</a>
{:else}
	<span
		class="inline-flex max-w-64 cursor-default items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10px]
			{TIER[evidence.trust_tier]?.cls ?? 'border-surface-600 text-slate-400'}"
		title="{evidence.reference}{evidence.digest ? ` · ${evidence.digest.slice(0, 16)}…` : ''}"
		data-testid="evidence-badge"
	>
		<span aria-hidden="true">{KIND_ICON[evidence.kind] ?? '•'}</span>
		<span class="truncate">{evidence.title}</span>
		{#if evidence.revision}
			<span class="text-slate-600">@{evidence.revision}</span>
		{/if}
	</span>
{/if}
