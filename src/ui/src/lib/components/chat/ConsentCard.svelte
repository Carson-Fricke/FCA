<script lang="ts">
	import { shortHash } from '$lib/format';
	import type { ConsentConfirmation, ConsentProposal } from '$lib/types';

	/**
	 * Renders the T172 activation grant proposal verbatim and captures the
	 * explicit structured confirmation. Free-text confirmation is
	 * structurally impossible: no text input exists in this card, and the
	 * confirm control re-states bundle hash, environment, and action.
	 */
	let {
		proposal,
		onConfirm,
		onReject,
		busy = false
	}: {
		proposal: ConsentProposal;
		onConfirm: (confirmation: ConsentConfirmation) => void;
		onReject?: () => void;
		busy?: boolean;
	} = $props();

	let acknowledged = $state(false);
	let confirmed = $state(false);

	const scopeText = $derived(
		proposal.scope.kind === 'one_shot' ? 'one-shot' : `interval until ${proposal.scope.intervalEnd}`
	);

	function confirmationToken(): string {
		const random =
			typeof crypto !== 'undefined' && 'randomUUID' in crypto
				? crypto.randomUUID().replaceAll('-', '')
				: Math.random().toString(16).slice(2).padEnd(32, '0');
		return `ctn-${random}`;
	}

	function confirm(): void {
		if (!acknowledged || confirmed || busy) return;
		confirmed = true;
		onConfirm({
			schema: 'fca-thesis-consent-confirmation-v1',
			action: proposal.action,
			bundleHash: proposal.bundleHash,
			environment: proposal.environment,
			confirmationToken: confirmationToken()
		});
	}
</script>

<div
	class="rounded-xl border border-warn/40 bg-surface-900 p-4"
	data-testid="consent-card"
	data-action={proposal.action}
	data-environment={proposal.environment}
>
	<div class="mb-3 flex items-center gap-2">
		<span class="h-2 w-2 rounded-full bg-warn" aria-hidden="true"></span>
		<h4 class="text-sm font-semibold text-warn">Activation consent required</h4>
		<span class="ml-auto font-mono text-[10px] text-slate-500">{proposal.schema}</span>
	</div>

	<dl class="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 font-mono text-xs">
		<dt class="text-slate-500">Bundle hash</dt>
		<dd class="text-slate-200" title={proposal.bundleHash}>{shortHash(proposal.bundleHash)}</dd>
		<dt class="text-slate-500">Environment</dt>
		<dd class="uppercase text-warn">{proposal.environment}</dd>
		<dt class="text-slate-500">Action</dt>
		<dd class="uppercase text-warn">{proposal.action}</dd>
		<dt class="text-slate-500">Instruments</dt>
		<dd class="text-slate-200">{proposal.instruments.join(', ')}</dd>
		<dt class="text-slate-500">Capital bound</dt>
		<dd class="text-slate-200">{proposal.capitalBound}</dd>
		<dt class="text-slate-500">Risk bound</dt>
		<dd class="text-slate-200">{proposal.riskBound} ({proposal.riskBoundKind})</dd>
		<dt class="text-slate-500">Expiry</dt>
		<dd class="text-slate-200">{proposal.expiration}</dd>
		<dt class="text-slate-500">Scope</dt>
		<dd class="text-slate-200">{scopeText}</dd>
	</dl>

	<label class="mt-4 flex items-start gap-2 text-xs text-slate-300">
		<input
			type="checkbox"
			class="mt-0.5 accent-cyan-400"
			bind:checked={acknowledged}
			disabled={confirmed || busy}
		/>
		<span>
			I confirm the proposal above exactly: bundle, environment
			<strong class="uppercase">{proposal.environment}</strong>, action
			<strong class="uppercase">{proposal.action}</strong>.
		</span>
	</label>

	<div class="mt-3 flex items-center gap-2">
		<button
			type="button"
			class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent transition hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
			disabled={!acknowledged || confirmed || busy}
			onclick={confirm}
			data-testid="consent-confirm"
		>
			{confirmed ? 'Confirmation recorded' : 'Confirm activation'}
		</button>
		{#if onReject && !confirmed}
			<button
				type="button"
				class="rounded-lg border border-surface-600 px-3 py-1.5 text-xs text-slate-400 hover:text-slate-200"
				onclick={onReject}
				disabled={busy}
			>
				Reject
			</button>
		{/if}
		<span class="ml-auto text-[10px] text-slate-500">Free-text confirmation does not exist.</span>
	</div>
</div>
