<script lang="ts">
	import { shortDate } from '$lib/format';
	import type { StrategyLineage } from '$lib/types';

	/** Strategy lineage graph: version ancestry rendered as depth columns. */
	let { lineage }: { lineage: StrategyLineage } = $props();

	/** Node depth = longest ancestry chain from a root (0 for roots). */
	const depths = $derived.by(() => {
		const depth = new Map<string, number>();
		const incoming = new Map<string, string[]>();
		for (const edge of lineage.edges) {
			incoming.set(edge.to, [...(incoming.get(edge.to) ?? []), edge.from]);
		}
		for (const node of lineage.nodes) {
			let d = 0;
			let cursor: string | undefined = node.id;
			const seen = new Set<string>();
			while (cursor) {
				const parents: string[] = incoming.get(cursor) ?? [];
				cursor = parents[0];
				if (!cursor || seen.has(cursor)) break;
				seen.add(cursor);
				d++;
			}
			depth.set(node.id, d);
		}
		return depth;
	});

	const columns = $derived.by(() => {
		const max = Math.max(0, ...depths.values());
		const cols: { node: StrategyLineage['nodes'][number]; relation: StrategyLineage['edges'][number] | null }[][] =
			Array.from({ length: max + 1 }, () => []);
		for (const node of lineage.nodes) {
			cols[depths.get(node.id) ?? 0].push({
				node,
				relation: lineage.edges.find((e) => e.to === node.id) ?? null
			});
		}
		return cols;
	});
</script>

<div
	class="rounded-xl border border-white/5 bg-surface-850/60 p-4"
	data-testid="lineage-graph"
	data-strategy={lineage.strategy_id}
>
	<h3 class="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-200">
		{lineage.strategy_name}
		<span class="font-mono text-[10px] text-slate-500">{lineage.strategy_id} · version ancestry</span>
	</h3>

	<div class="flex items-stretch gap-6 overflow-x-auto">
		{#each columns as column, depth (depth)}
			<div class="relative flex min-w-52 flex-col gap-2">
				{#if depth > 0}
					<div
						class="absolute top-0 bottom-0 -left-3 border-l border-dashed border-surface-600/70"
						aria-hidden="true"
					></div>
				{/if}
			{#each column as entry (entry.node.id)}
				<div
					class="rounded-lg border border-white/10 bg-surface-900/70 px-3 py-2"
					data-testid="lineage-node"
					data-version={entry.node.version}
				>
					<div class="flex items-baseline gap-2">
						<span class="font-mono text-xs font-semibold text-accent">v{entry.node.version}</span>
						<span class="ml-auto font-mono text-[10px] text-slate-600">
							{shortDate(entry.node.created_at)}
						</span>
					</div>
					<p class="mt-0.5 text-[11px] text-slate-400">{entry.node.note}</p>
					{#if entry.relation}
						<p class="mt-1 font-mono text-[10px] text-slate-600">
							↳ {entry.relation.relation.replace('-', ' ')} {entry.relation.from.split('@')[1]}
						</p>
					{/if}
				</div>
			{/each}
			</div>
		{/each}
	</div>
</div>
