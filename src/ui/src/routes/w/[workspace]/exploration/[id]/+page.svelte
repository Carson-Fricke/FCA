<script lang="ts">
	import { page } from '$app/state';
	import { apiBaseUrl, getExploratoryOutput, type ExploratoryOutputPage } from '$lib/api';
	const jobId = $derived(page.params.id ?? '');
	let catalog = $state<ExploratoryOutputPage | null>(null);
	let detail = $state<ExploratoryOutputPage | null>(null);
	let error = $state('');
	let selection = $state<{ instanceId: string; name: string } | null>(null);
	let offset = $state(0);
	let loading = $state(false);
	$effect(() => {
		let cancelled = false;
		getExploratoryOutput(jobId).then((result) => { if (!cancelled) catalog = result; }).catch((cause) => { if (!cancelled) error = String(cause); });
		return () => { cancelled = true; };
	});
	async function inspect(instanceId: string, name: string, start = 0) {
		loading = true; error = ''; selection = { instanceId, name }; offset = start;
		try { detail = await getExploratoryOutput(jobId, instanceId, name, start); } catch (cause) { error = String(cause); } finally { loading = false; }
	}
	const records = $derived.by(() => {
		const output = detail?.output;
		const rows = output?.points ?? output?.events ?? output?.rows;
		return Array.isArray(rows) ? rows : [];
	});
	const points = $derived.by(() => records.flatMap((record, index) => {
		if (!record || typeof record !== 'object' || !('value' in record) || typeof record.value !== 'number') return [];
		return [{ x: index, y: record.value }];
	}));
	const plot = $derived.by(() => {
		if (!points.length) return '';
		const minimum = Math.min(...points.map((point) => point.y));
		const maximum = Math.max(...points.map((point) => point.y));
		return points.map((point) => `${10 + point.x / Math.max(1, records.length - 1) * 780},${190 - (point.y - minimum) / Math.max(1e-12, maximum - minimum) * 180}`).join(' ');
	});
</script>

<svelte:head><title>Exploratory outputs — Thesis</title></svelte:head>
<div class="mx-auto max-w-6xl space-y-4">
	<h1 class="text-xl font-semibold">Exploratory outputs</h1>
	<p class="font-mono text-xs">{jobId}</p>
	<a class="text-sm text-accent" href={`${apiBaseUrl}/v1/jobs/${encodeURIComponent(jobId)}/artifacts/exploratory_outputs.json`} download="exploratory_outputs.json">Export complete output artifact</a>
	{#if error}<p role="alert" class="text-loss">{error}</p>{/if}
	{#if catalog}
		<p class="text-xs text-slate-500">{catalog.artifactName} · SHA-256 {catalog.artifactSha256}</p>
		<div class="flex flex-wrap gap-2">{#each catalog.outputs ?? [] as output}
			<button class="rounded border border-surface-600 p-2 text-sm" onclick={() => inspect(output.instanceId, output.name)} disabled={loading}>{output.instanceId} / {output.name} · {output.kind} · {output.count ?? 'state'}</button>
		{/each}</div>
	{/if}
	{#if detail}
		<h2 class="text-lg">{selection?.name}</h2>
		{#if plot}<svg viewBox="0 0 800 200" class="w-full rounded border border-surface-600" role="img" aria-label="Observed series values in this page; missing values remain in the records below"><polyline points={plot} fill="none" stroke="currentColor" stroke-width="2" /></svg>{/if}
		<p class="text-xs">Records {offset + 1}–{offset + records.length} of {detail.total ?? 'state'}. Availability times, interval identity and undefined reasons are preserved below.</p>
		<pre class="max-h-[36rem] overflow-auto rounded border border-surface-600 p-3 text-xs">{JSON.stringify(detail.output, null, 2)}</pre>
		{#if selection}<div class="flex gap-3"><button disabled={loading || offset === 0} onclick={() => inspect(selection!.instanceId, selection!.name, Math.max(0, offset - 100))}>Previous</button><button disabled={loading || detail.nextOffset === null} onclick={() => inspect(selection!.instanceId, selection!.name, detail!.nextOffset!)}>Next</button></div>{/if}
	{/if}
</div>
