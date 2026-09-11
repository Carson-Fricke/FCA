<script lang="ts">
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import { getValidation, launchValidation, listValidations } from '$lib/api';
	import { relativeTime } from '$lib/format';
	import type { ValidationEvidenceItem, ValidationRecord, ValidationStatus, ValidationVerdict } from '$lib/types';
	import { notify } from '$lib/workspace.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let validations = $state<ValidationRecord[]>([]);
	let selectedId = $state<string | null>(null);
	let detail = $state<ValidationRecord | null>(null);
	let claim = $state('');
	let launching = $state(false);
	let loading = $state(false);
	// Non-reactive so the list effect does not re-run when a row is selected.
	let initialized = false;

	$effect(() => {
		let cancelled = false;
		listValidations(workspaceId).then((items) => {
			if (cancelled) return;
			validations = items;
			if (!initialized && items.length) {
				initialized = true;
				selectedId = items[0].id;
			}
		});
		return () => {
			cancelled = true;
		};
	});

	$effect(() => {
		const id = selectedId;
		if (!id) {
			detail = null;
			return;
		}
		let cancelled = false;
		loading = true;
		getValidation(workspaceId, id)
			.then((record) => {
				if (!cancelled) detail = record;
			})
			.catch(() => {
				if (!cancelled) detail = null;
			})
			.finally(() => {
				if (!cancelled) loading = false;
			});
		return () => {
			cancelled = true;
		};
	});

	async function submit(): Promise<void> {
		const trimmed = claim.trim();
		if (!trimmed || launching) return;
		launching = true;
		try {
			const created = await launchValidation(workspaceId, trimmed);
			validations = [created, ...validations];
			detail = created;
			selectedId = created.id;
			claim = '';
			notify(`Validation “${created.id}” launched`);
		} catch (error) {
			notify(
				`Validation launch failed: ${error instanceof Error ? error.message : String(error)}`,
				true
			);
		} finally {
			launching = false;
		}
	}

	const STATUS_TONE: Record<ValidationStatus, string> = {
		completed: 'border-profit/40 text-profit',
		running: 'border-accent/40 text-accent',
		queued: 'border-surface-600 text-slate-500',
		failed: 'border-loss/40 text-loss'
	};

	const VERDICT_TONE: Record<ValidationVerdict, string> = {
		supported: 'border-profit/40 bg-profit/10 text-profit',
		refuted: 'border-loss/40 bg-loss/10 text-loss',
		inconclusive: 'border-warn/40 bg-warn/10 text-warn',
		pending: 'border-surface-600 bg-surface-800 text-slate-500'
	};

	/** Route an evidence atom to the run or artifact that produced it. */
	function evidenceHref(item: ValidationEvidenceItem): string | null {
		if (item.run_id) return `/w/${workspaceId}/backtests/${encodeURIComponent(item.run_id)}`;
		if (item.artifact && /^https?:\/\//i.test(item.artifact)) return item.artifact;
		if (item.reference && /^https?:\/\//i.test(item.reference)) return item.reference;
		return null;
	}
</script>

<svelte:head><title>Validation — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-4">
	<header>
		<h1 class="text-xl font-semibold text-slate-800">Hypothesis validation</h1>
		<p class="text-sm text-slate-500">
			Launch a validation workflow and inspect its contract, verdict, and evidence.
		</p>
	</header>

	<form
		class="rounded-xl border border-surface-700 bg-surface-900 p-4"
		onsubmit={(event) => {
			event.preventDefault();
			submit();
		}}
	>
		<label class="block text-xs font-semibold text-slate-700" for="validation-claim">Claim</label>
		<textarea
			id="validation-claim"
			data-testid="validation-claim"
			rows="2"
			class="mt-1 w-full resize-y rounded-lg border border-surface-600 bg-surface-950 px-3 py-2 text-sm text-slate-800 outline-none focus:border-accent/50"
			placeholder="e.g. Momentum signals persist over a 20-day horizon"
			bind:value={claim}
		></textarea>
		<div class="mt-2 flex items-center justify-end">
			<button
				type="submit"
				data-testid="launch-validation"
				disabled={launching || claim.trim().length === 0}
				class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-60"
			>
				{launching ? 'Launching…' : '＋ Launch validation'}
			</button>
		</div>
	</form>

	<div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
		<section class="space-y-2" data-testid="validation-list">
			<h2 class="text-sm font-semibold text-slate-800">Validations</h2>
			{#if validations.length === 0}
				<EmptyState
					title="No validations yet"
					hint="Submit a claim above to launch your first validation."
				/>
			{:else}
				<ul class="space-y-2">
					{#each validations as record (record.id)}
						<li>
							<button
								type="button"
								data-testid="validation-row"
								class="w-full rounded-xl border p-3 text-left transition
									{record.id === selectedId
									? 'border-accent/40 bg-accent/10'
									: 'border-surface-700 bg-surface-900 hover:border-surface-600'}"
								onclick={() => (selectedId = record.id)}
							>
								<div class="flex items-center gap-2">
									<span
										class="rounded-full border px-2 py-0.5 font-mono text-[10px] {STATUS_TONE[
											record.status
										]}"
									>
										{record.status}
									</span>
									<span class="ml-auto font-mono text-[10px] text-slate-500"
										>{relativeTime(record.created_at)}</span
									>
								</div>
								<p class="mt-1.5 line-clamp-2 text-sm text-slate-700">{record.claim}</p>
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<section class="space-y-3">
			{#if detail}
				{@const record = detail}
				<div
					class="rounded-xl border border-surface-700 bg-surface-900 p-4"
					data-testid="validation-detail"
				>
					<header class="flex flex-wrap items-center gap-2">
						<h2 class="text-sm font-semibold text-slate-800">{record.claim}</h2>
						<span
							class="ml-auto rounded-full border px-2.5 py-0.5 font-mono text-[10px] {VERDICT_TONE[
								record.verdict
							]}"
							data-testid="validation-verdict"
						>
							{record.verdict}
						</span>
					</header>

					{#if record.contract}
						{@const contract = record.contract}
						<div
							class="mt-3 rounded-lg border border-surface-700 bg-surface-950 p-3"
							data-testid="validation-contract"
						>
							<h3 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
								Contract
							</h3>
							<p class="mt-1 text-sm text-slate-700">{contract.hypothesis}</p>
							<dl class="mt-2 grid grid-cols-2 gap-2 font-mono text-[11px] sm:grid-cols-3">
								<div>
									<dt class="text-slate-500">metric</dt>
									<dd class="text-slate-700">{contract.metric ?? '—'}</dd>
								</div>
								<div>
									<dt class="text-slate-500">threshold</dt>
									<dd class="text-slate-700">{contract.threshold ?? '—'}</dd>
								</div>
								<div>
									<dt class="text-slate-500">direction</dt>
									<dd class="text-slate-700">{contract.direction ?? '—'}</dd>
								</div>
							</dl>
							{#if contract.evidence_requirements.length}
								<div class="mt-2 flex flex-wrap gap-1.5">
									{#each contract.evidence_requirements as requirement (requirement)}
										<span
											class="rounded-full border border-surface-600 bg-surface-800 px-2 py-0.5 text-[10px] text-slate-500"
										>
											{requirement}
										</span>
									{/each}
								</div>
							{/if}
						</div>
					{/if}

					<div class="mt-4" data-testid="validation-evidence">
						<h3 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
							Evidence <span class="font-mono">({record.evidence.length})</span>
						</h3>
						<ul class="mt-2 space-y-1.5">
							{#each record.evidence as item (item.id)}
								{@const href = evidenceHref(item)}
								<li
									class="flex flex-wrap items-center gap-2 rounded-lg border border-surface-700 bg-surface-950 px-3 py-2"
									data-testid="evidence-item"
								>
									<span
										class="rounded border border-surface-600 px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
										>{item.kind}</span
									>
									{#if href}
										<a
											href={href}
											class="text-sm text-accent hover:underline"
											data-testid="evidence-link">{item.title}</a
										>
									{:else}
										<span class="text-sm text-slate-700">{item.title}</span>
									{/if}
									<span class="ml-auto font-mono text-[10px] text-slate-500">{item.reference}</span>
								</li>
							{:else}
								<li class="text-xs text-slate-500">No evidence recorded yet.</li>
							{/each}
						</ul>
					</div>
				</div>
			{:else if loading}
				<div class="h-48 animate-pulse rounded-xl border border-surface-700 bg-surface-900"></div>
			{:else}
				<EmptyState
					title="Select a validation"
					hint="Its contract, verdict, and evidence appear here."
				/>
			{/if}
		</section>
	</div>
</div>
