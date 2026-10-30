<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type { OutlineGroup, OutlineEntry } from '$lib/conversation-outline';
	let { groups, loading = false, hasMore = false, error = '', selectedId = '', onSelect, onRetry }: {
		groups: OutlineGroup[]; loading?: boolean; hasMore?: boolean; error?: string; selectedId?: string;
		onSelect: (entry: OutlineEntry) => void; onRetry: () => void;
	} = $props();
	let panel = $state<HTMLElement>();
	let width = $state(260);
	let resizing = $state(false);
	let dragStart = 0;
	let dragWidth = 0;
	let pendingWidth = 260;
	const widthKey = 'thesis.conversation-outline.width';
	function limit(): number { return Math.max(210, Math.min(480, (panel?.parentElement?.clientWidth ?? 800) - 320)); }
	function commit(next: number): void {
		width = Math.min(limit(), Math.max(210, next));
		if (panel) panel.style.width = `${width}px`;
		try { localStorage.setItem(widthKey, String(width)); } catch { /* Presentation preference only. */ }
	}
	function beginResize(event: PointerEvent): void {
		if (!panel) return;
		event.preventDefault();
		dragStart = event.clientX;
		dragWidth = panel.getBoundingClientRect().width;
		pendingWidth = dragWidth;
		resizing = true;
		window.addEventListener('pointermove', moveResize);
		window.addEventListener('pointerup', endResize);
		window.addEventListener('pointercancel', endResize);
	}
	function moveResize(event: PointerEvent): void {
		pendingWidth = Math.max(210, Math.min(limit(), dragWidth + dragStart - event.clientX));
		if (panel) panel.style.width = `${pendingWidth}px`;
	}
	function endResize(): void {
		window.removeEventListener('pointermove', moveResize);
		window.removeEventListener('pointerup', endResize);
		window.removeEventListener('pointercancel', endResize);
		if (resizing) commit(pendingWidth);
		resizing = false;
	}
	function resizeKey(event: KeyboardEvent): void {
		const next = event.key === 'ArrowLeft' ? width + 20 : event.key === 'ArrowRight' ? width - 20 : event.key === 'Home' ? 210 : event.key === 'End' ? limit() : null;
		if (next === null) return;
		event.preventDefault();
		commit(next);
	}
	onMount(() => {
		try { const saved = Number(localStorage.getItem(widthKey)); width = Math.min(limit(), Number.isFinite(saved) && saved >= 210 ? saved : 260); } catch { /* Best effort. */ }
	});
	onDestroy(endResize);
	const title = (entry: OutlineEntry) => entry.kind === 'workflow' ? entry.title.replace(/[-_]/g, ' ').replace(/^./, (letter) => letter.toUpperCase()) : entry.title;
	const label = (entry: OutlineEntry) => `${title(entry)}, ${entry.kind}${entry.status ? `, ${entry.status}` : ''}`;
	const totals = $derived(groups.flatMap((group) => group.entries).reduce((counts, entry) => {
		counts[entry.kind] = (counts[entry.kind] ?? 0) + 1;
		return counts;
	}, {} as Record<string, number>));
	const countLabel = $derived(['workflow', 'study', 'artifact'].filter((kind) => totals[kind])
		.map((kind) => `${totals[kind]} ${totals[kind] === 1 ? kind : kind === 'study' ? 'studies' : `${kind}s`}`).join(' · '));
</script>

<aside bind:this={panel} class="conversation-outline" class:resizing style:width={`${width}px`} aria-label="In this conversation" data-testid="conversation-outline">
	<!-- Adjustable separators are focusable widgets with pointer and keyboard resizing. -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div class="outline-resize" role="separator" tabindex="0" aria-label="Resize conversation outline" aria-orientation="vertical" aria-valuemin="210" aria-valuemax={limit()} aria-valuenow={width} onpointerdown={beginResize} onkeydown={resizeKey} data-testid="outline-resize-handle"><span></span></div>
	<div class="outline-scroll">
	<header><h2>In this conversation</h2><p>{countLabel}</p></header>
	<div class="outline-groups">
		{#each groups as group (group.id)}
			<details class="outline-turn" open><summary title={group.title}>{group.title}</summary>
				{@render rows(group, null)}
			</details>
		{/each}
		{#if loading}<p class="outline-notice" role="status">Loading outline…</p>{:else if !groups.length && !error}<p class="outline-notice">Workflows, studies, and artifacts will appear here.</p>{/if}
		{#if error}<p class="outline-notice" role="status">{error}</p><button type="button" class="outline-retry" onclick={onRetry}>Retry outline</button>{/if}
		{#if hasMore && !loading}<button type="button" class="outline-retry" onclick={onRetry}>Load more outline</button>{/if}
	</div>
	</div>
</aside>

{#snippet rows(group: OutlineGroup, parentId: string | null)}
	{#each group.entries.filter((entry) => entry.parentId === parentId) as entry (entry.id)}
		{@const indent = 6 + Math.min(entry.depth, 4) * 10}
		{#if entry.kind === 'workflow' || entry.kind === 'conversation'}
			<details class="outline-branch" open>
				<summary class="outline-row" class:selected={selectedId === entry.id} style:padding-left={`${indent}px`} aria-label={`Toggle ${title(entry)}`} data-outline-id={entry.id} data-testid="outline-item">
					{@render entryIcon(entry.kind)}
					<button type="button" class="row-title" onclick={(event) => { event.preventDefault(); event.stopPropagation(); onSelect(entry); }} aria-current={selectedId === entry.id ? 'location' : undefined} aria-label={label(entry)} title={`${title(entry)} · Open conversation location`}>{title(entry)}</button>
					{@render status(entry)}
				</summary>
				{@render rows(group, entry.id)}
			</details>
		{:else}
			<div class="outline-row" class:selected={selectedId === entry.id} data-outline-id={entry.id} data-testid="outline-item">
				<button type="button" class="outline-leaf" style:padding-left={`${indent}px`} onclick={() => onSelect(entry)} aria-current={selectedId === entry.id ? 'location' : undefined} aria-label={label(entry)} title={label(entry)}>{@render entryIcon(entry.kind)}<span>{title(entry)}</span></button>
				{@render status(entry)}
			</div>
		{/if}
	{/each}
{/snippet}

{#snippet status(entry: OutlineEntry)}
	{#if entry.status}<span class="outline-status" class:active={['running', 'submitted', 'queued', 'launched', 'started'].includes(entry.status)} class:failed={['failed', 'cancelled'].includes(entry.status)} aria-hidden="true">{['completed', 'succeeded'].includes(entry.status) ? '✓' : ['failed', 'cancelled'].includes(entry.status) ? '!' : '◉'}</span>{/if}
{/snippet}

{#snippet entryIcon(kind: OutlineEntry['kind'])}
	<svg class="entry-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
		{#if kind === 'workflow'}<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M6.5 10v7.5H14"/>
		{:else if kind === 'conversation'}<path d="M4 4h16v12H9l-5 4z" stroke-linejoin="round"/>
		{:else if kind === 'study'}<path d="M9 3h6m-5 0v7L4 20h16l-6-10V3M7 15h10" stroke-linejoin="round"/>
		{:else}<path d="M5 3h9l5 5v13H5zM14 3v6h5M8 13h8m-8 4h6" stroke-linejoin="round"/>{/if}
	</svg>
{/snippet}

<style>
	.conversation-outline{position:relative;flex-shrink:0;min-height:0;min-width:0;border-left:1px solid var(--color-surface-700);background:var(--color-surface-800);color:var(--color-slate-200)}.outline-scroll{height:100%;overflow-y:auto;padding:15px 8px}.resizing{user-select:none}
	.outline-resize{position:absolute;inset:0 auto 0 -4px;width:8px;z-index:10;display:flex;justify-content:center;align-items:center;cursor:col-resize;touch-action:none}.outline-resize>span{height:32px;width:3px;border-radius:3px;background:var(--color-surface-600)}.outline-resize:hover>span,.outline-resize:focus-visible>span{background:var(--color-accent)}
	header{padding:0 5px;margin-bottom:18px}h2{font-size:12px;font-weight:500;margin:0}header p{font-size:11px;color:var(--color-slate-400);margin:4px 0 0}
	.outline-turn{margin-bottom:12px}summary{list-style:none;font-size:11px;color:var(--color-slate-400);padding:0 4px 5px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden;cursor:pointer}summary::-webkit-details-marker{display:none}summary::before{content:'▸';margin-right:5px;flex-shrink:0;font-size:10px}details[open]>summary::before{content:'▾'}
	.outline-row{position:relative;display:flex;align-items:center;gap:6px;height:24px;width:100%;min-width:0;border-radius:4px;font-size:12px;padding:0}.outline-row:hover,.outline-row.selected{background:color-mix(in srgb,var(--color-accent) 9%,var(--color-surface-900))}.outline-branch>summary{color:var(--color-slate-200);padding:3px 20px 3px 6px}.outline-branch>summary::before{margin-right:0}
	.row-title{flex:0 1 auto;min-width:0;width:max-content;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;text-align:left;padding:0}.row-title:hover{text-decoration:underline;text-underline-offset:3px;color:var(--color-accent)}
	.outline-leaf{display:flex;align-items:center;gap:6px;width:100%;height:100%;padding:3px 20px 3px 6px;text-align:left;cursor:pointer;border-radius:4px}.outline-leaf>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}.entry-icon{width:13px;height:13px;flex-shrink:0;color:var(--color-slate-400)}
	.outline-status{position:absolute;right:6px;pointer-events:none;font-size:11px;color:var(--color-slate-400)}.outline-status.active{color:var(--color-accent)}.outline-status.failed{color:var(--color-loss)}
	.outline-notice,.outline-retry{font-size:11px;color:var(--color-slate-400);padding:4px;overflow-wrap:anywhere}.outline-retry{color:var(--color-accent)}
	@media(max-width:700px){.conversation-outline{max-width:45%}.outline-scroll{padding-left:5px;padding-right:5px}}
	@media(max-width:480px){.conversation-outline{width:100%!important;max-width:100%;height:180px;max-height:180px;overflow:hidden;border-left:0;border-top:1px solid var(--color-surface-700)}.outline-resize{display:none}}
	@media(pointer:coarse){.outline-row{height:44px}}
</style>
