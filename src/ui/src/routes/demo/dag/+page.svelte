<script>
	import TaskDag from '$lib/components/task-dag/TaskDag.svelte';
	import { nodes } from '$lib/data/dagFixture.js';

	let selected = $state(null);

	function pick(node) {
		selected = node;
	}
</script>

<svelte:head>
	<title>CLEO task DAG demo - FCA Thesis</title>
</svelte:head>

<div class="page">
	<h1>CLEO task DAG</h1>
	<p class="sub">
		Workspace plan sample: {nodes.length} nodes. Click any node to inspect it. Fixture data, no
		backend.
	</p>
	<div class="grid">
		<TaskDag nodes={nodes} selected={selected} onselect={pick} height={640} />
		<aside class="detail">
			{#if selected}
				<h2>{selected.id}</h2>
				<dl>
					<dt>Title</dt>
					<dd>{selected.title}</dd>
					<dt>Type / status</dt>
					<dd>{selected.type} &middot; {selected.status}</dd>
					<dt>Priority</dt>
					<dd>{selected.priority}</dd>
					<dt>Parent</dt>
					<dd>{selected.parentId ?? 'none'}</dd>
					<dt>Depends on</dt>
					<dd>{selected.depends?.length ? selected.depends.join(', ') : 'none'}</dd>
					<dt>Files</dt>
					<dd>{selected.files?.length ? selected.files.join(', ') : 'none'}</dd>
					<dt>Notes</dt>
					<dd>{selected.notes || 'none'}</dd>
				</dl>
			{:else}
				<p class="empty">Click a node in the graph to inspect it.</p>
			{/if}
		</aside>
	</div>
</div>

<style>
	.grid {
		display: grid;
		grid-template-columns: 1fr 300px;
		gap: 16px;
		align-items: start;
	}
	.detail {
		background: var(--panel);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 14px;
		font-size: 13px;
		min-height: 200px;
	}
	.detail h2 {
		margin: 0 0 10px;
		font-size: 15px;
		color: var(--accent);
	}
	dl {
		margin: 0;
		display: grid;
		grid-template-columns: 90px 1fr;
		row-gap: 6px;
	}
	dt {
		color: var(--muted);
	}
	dd {
		margin: 0;
	}
	.empty {
		color: var(--muted);
	}
	@media (max-width: 900px) {
		.grid {
			grid-template-columns: 1fr;
		}
	}
</style>
