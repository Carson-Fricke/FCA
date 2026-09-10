<script lang="ts">
	import { shortDate } from '$lib/format';
	import type { ConsentAuditEntry } from '$lib/types';

	/** Consent/audit trail: proposals, confirmations, grants, revocations. */
	let { entries }: { entries: ConsentAuditEntry[] } = $props();

	const KIND_TONE: Record<string, string> = {
		proposal: 'border-warn/40 text-warn',
		confirmation: 'border-accent/40 text-accent',
		grant: 'border-profit/40 text-profit',
		revocation: 'border-loss/40 text-loss'
	};
</script>

<div class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="consent-audit">
	<h3 class="mb-3 text-sm font-semibold text-slate-200">
		Consent &amp; audit trail
		<span class="font-mono text-[10px] text-slate-500">structured confirmation only</span>
	</h3>
	{#if entries.length}
		<ol class="space-y-1.5">
			{#each entries as entry (entry.grantId + entry.kind + entry.at)}
				<li class="flex flex-wrap items-center gap-2 rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 text-xs">
					<span class="font-mono text-slate-600">{shortDate(entry.at)}</span>
					<span class="rounded-full border px-2 py-0.5 font-mono text-[10px] {KIND_TONE[entry.kind] ?? 'border-surface-600 text-slate-400'}">
						{entry.kind}
					</span>
					<span class="font-mono text-slate-400">{entry.environment}/{entry.action}</span>
					<span class="text-slate-400">{entry.summary}</span>
					<span class="ml-auto font-mono text-[10px] text-slate-600">{entry.grantId}</span>
				</li>
			{/each}
		</ol>
	{:else}
		<p class="text-xs text-slate-600">No consent events recorded for this workspace.</p>
	{/if}
</div>
