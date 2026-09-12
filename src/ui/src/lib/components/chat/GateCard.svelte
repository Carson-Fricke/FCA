<script lang="ts">
	import type { GateResolution, WorkflowGate } from '$lib/types';

	/**
	 * Transcript item of kind `gate`. The gate reason is rendered verbatim
	 * (never paraphrased) and the operator resolves it through one of three
	 * structured paths, which the host posts back to resume the workflow.
	 */
	let {
		gate,
		busy = false,
		onResolve = () => {}
	}: {
		gate: WorkflowGate;
		busy?: boolean;
		onResolve?: (resolution: GateResolution) => void;
	} = $props();

	let disagreement = $state('');
	let verdict = $state('accept');

	const resolved = $derived(gate.status === 'resolved');
	const resolutionLabel = $derived(
		gate.resolution
			? `${gate.resolution.kind.replace('_', ' ')}${gate.resolution.verdict ? ` · ${gate.resolution.verdict}` : ''}`
			: ''
	);
</script>

<section
	class="rounded-xl border {resolved ? 'border-profit/30 bg-profit/5' : 'border-warn/40 bg-warn/5'} p-3"
	data-testid="gate-card"
	data-status={gate.status}
	data-gate={gate.gateId}
>
	<header class="mb-1 flex flex-wrap items-center gap-2">
		<span class="text-[10px] font-semibold tracking-wider text-warn uppercase">workflow gate</span>
		{#if gate.phase}
			<span class="font-mono text-[10px] text-slate-500">phase {gate.phase}</span>
		{/if}
		<span class="ml-auto font-mono text-[10px] text-slate-500">{gate.gateId}</span>
	</header>

	<p class="text-sm whitespace-pre-wrap text-slate-800" data-testid="gate-reason">{gate.reason}</p>

	{#if resolved}
		<p class="mt-2 text-xs text-profit" data-testid="gate-resolved">
			Resolved · {resolutionLabel}
			{#if gate.resolution?.note}
				— {gate.resolution.note}
			{/if}
		</p>
	{:else}
		<div class="mt-2.5 space-y-2.5" data-testid="gate-resolution">
			<button
				type="button"
				class="rounded-lg border border-profit/40 bg-profit/10 px-2.5 py-1 text-xs font-semibold text-profit transition hover:bg-profit/20 disabled:opacity-40"
				disabled={busy}
				onclick={() => onResolve({ kind: 'approve_plan' })}
				data-testid="gate-approve"
			>
				Approve plan
			</button>

			<div class="space-y-1.5" data-testid="gate-disagreement">
				<label
					class="block text-[10px] tracking-wider text-slate-500 uppercase"
					for="gate-note-{gate.gateId}"
				>
					Add disagreement
				</label>
				<textarea
					id="gate-note-{gate.gateId}"
					class="min-h-9 w-full resize-y rounded-lg border border-surface-600/60 bg-surface-950 px-2.5 py-1.5 text-xs text-slate-800 outline-none focus:border-warn/50"
					placeholder="State the disagreement, then submit it to resume the workflow…"
					bind:value={disagreement}
					disabled={busy}
					data-testid="gate-disagreement-note"
				></textarea>
				<button
					type="button"
					class="rounded-lg border border-warn/40 bg-warn/10 px-2.5 py-1 text-xs font-semibold text-warn transition hover:bg-warn/20 disabled:opacity-40"
					disabled={busy || !disagreement.trim()}
					onclick={() => onResolve({ kind: 'disagreement', note: disagreement.trim() })}
					data-testid="gate-disagreement-submit"
				>
					Submit disagreement
				</button>
			</div>

			<div class="flex flex-wrap items-end gap-2" data-testid="gate-verdict">
				<label
					class="text-[10px] tracking-wider text-slate-500 uppercase"
					for="gate-verdict-{gate.gateId}"
				>
					Verdict
				</label>
				<select
					id="gate-verdict-{gate.gateId}"
					class="rounded-lg border border-surface-600/60 bg-surface-950 px-2 py-1 text-xs text-slate-800 outline-none focus:border-accent/50"
					bind:value={verdict}
					disabled={busy}
					data-testid="gate-verdict-select"
				>
					<option value="accept">Accept</option>
					<option value="return">Return</option>
				</select>
				<button
					type="button"
					class="rounded-lg border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs font-semibold text-accent transition hover:bg-accent/20 disabled:opacity-40"
					disabled={busy}
					onclick={() => onResolve({ kind: 'verdict', verdict })}
					data-testid="gate-verdict-submit"
				>
					Submit verdict
				</button>
			</div>
		</div>
	{/if}
</section>
