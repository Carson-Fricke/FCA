<script lang="ts">
	import FalsificationReportCard from './FalsificationReportCard.svelte';
	import type { PhaseAttemptRun } from '$lib/subagent';
	import type { FalsificationReportData } from '$lib/transcript';

	/**
	 * The phase run view (REQ-D2): the surface a deterministic phase's scope
	 * segment renders. It is always read-only — a run log of live
	 * `workflow.progress` lines, then the `workflow.phase_result` rendered
	 * through the typed falsification card when the artifact is one, an
	 * error variant otherwise. No composer.
	 *
	 * A phase that looped can carry several attempts: the selector shows one
	 * attempt's log and result at a time, defaulting to the latest.
	 */
	let {
		attempts = [],
		selectedAttempt = null,
		onSelectAttempt = () => {}
	}: {
		/** Every recorded attempt of this phase, oldest first. */
		attempts?: PhaseAttemptRun[];
		/** The attempt currently displayed; defaults to the latest. */
		selectedAttempt?: number | null;
		onSelectAttempt?: (attempt: number) => void;
	} = $props();

	const current = $derived(
		attempts.find((run) => run.attempt === selectedAttempt) ??
			attempts[attempts.length - 1] ??
			null
	);
	const progress = $derived(current?.progress ?? []);
	const result = $derived(current?.result ?? null);
	const error = $derived(current?.error ?? null);
	const latestAttempt = $derived(attempts[attempts.length - 1]?.attempt ?? null);

	function isFalsificationArtifact(artifact: Record<string, unknown>): boolean {
		return (
			artifact['type'] === 'falsification_report' ||
			artifact['type'] === 'falsification_trials' ||
			(Array.isArray(artifact['trials']) && artifact['trials'].length > 0)
		);
	}

	const report = $derived(
		result && isFalsificationArtifact(result.artifact)
			? (result.artifact as unknown as FalsificationReportData)
			: null
	);

	/**
	 * The operator-visible failure message. A failed computation record takes
	 * precedence; otherwise the backend's error threading completes the phase
	 * with a `{ type: 'error', message }` artifact, which must render through
	 * the same error variant.
	 */
	const failureMessage = $derived(
		error?.message ||
			(result &&
			result.artifact['type'] === 'error' &&
			typeof result.artifact['message'] === 'string' &&
			result.artifact['message'].length > 0
				? result.artifact['message']
				: null)
	);
</script>

<section
	class="flex flex-col gap-3 overflow-hidden rounded-xl border border-surface-700 bg-surface-900"
	data-testid="phase-run-view"
>
	<header class="border-b border-surface-700 bg-surface-950/40 p-3.5">
		<div class="flex flex-wrap items-center justify-between gap-2">
			<h3 class="text-sm font-semibold text-slate-800">Phase run</h3>
			<div class="flex items-center gap-2">
				{#if attempts.length > 1}
					<label
						class="flex items-center gap-1.5 font-mono text-[10px] font-semibold tracking-wide text-slate-500 uppercase"
					>
						Attempt
						<select
							class="rounded border border-surface-600 bg-surface-950 px-1.5 py-0.5 font-mono text-[10px] text-slate-700 outline-none"
							value={current?.attempt ?? ''}
							onchange={(event) =>
								onSelectAttempt(Number((event.target as HTMLSelectElement).value))}
							data-testid="phase-attempt-select"
							aria-label="Phase attempt"
						>
							{#each attempts as run (run.attempt)}
								<option value={run.attempt}>
									Attempt {run.attempt}{run.attempt === latestAttempt ? ' (latest)' : ''}
								</option>
							{/each}
						</select>
					</label>
				{/if}
				<span
					class="rounded-full border px-2.5 py-0.5 font-mono text-[10px] font-semibold uppercase {failureMessage
						? 'border-loss/40 bg-loss/10 text-loss'
						: report
							? 'border-profit/40 bg-profit/10 text-profit'
							: 'border-accent/40 bg-accent/10 text-accent'}"
					data-testid="phase-run-status"
				>
					{failureMessage ? 'Failed' : report ? 'Completed' : 'Running'}
				</span>
			</div>
		</div>
	</header>

	{#if progress.length}
		<ol class="space-y-1.5 px-3.5" data-testid="phase-run-log" aria-label="Phase progress">
			{#each progress as line, index (index)}
				<li
					class="flex items-start gap-2 font-mono text-[11px] text-slate-500"
					data-testid="phase-progress-line"
				>
					<span class="shrink-0 opacity-70">{line.at}</span>
					<span class="min-w-0 break-words text-slate-600">{line.text}</span>
				</li>
			{/each}
		</ol>
	{:else}
		<p class="px-3.5 font-mono text-[11px] text-slate-500" data-testid="phase-run-empty">
			{attempts.length
				? 'No progress recorded for this attempt.'
				: 'No progress recorded for this phase yet.'}
		</p>
	{/if}

	{#if failureMessage}
		<div
			class="mx-3.5 mb-3.5 rounded-lg border border-loss/40 bg-loss/10 p-3"
			data-testid="phase-run-error"
		>
			<p class="font-mono text-[10px] font-semibold tracking-wide text-loss uppercase">
				Phase failed
			</p>
			<p class="mt-1 text-xs text-slate-600" data-testid="phase-run-error-message">
				{failureMessage}
			</p>
		</div>
	{:else if report}
		<div class="mx-3.5 mb-3.5" data-testid="phase-run-result">
			<FalsificationReportCard data={report} />
		</div>
	{:else if result}
		<div
			class="mx-3.5 mb-3.5 rounded-lg border border-surface-700 bg-surface-950/30 p-3"
			data-testid="phase-run-result"
		>
			<p class="font-mono text-[10px] font-semibold tracking-wide text-slate-500 uppercase">
				Result
			</p>
			<p class="mt-1 text-xs text-slate-600">{result.summary}</p>
		</div>
	{/if}
</section>
