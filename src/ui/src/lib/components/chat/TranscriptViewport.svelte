<script lang="ts">
	import { tick, untrack, type Snippet } from 'svelte';

	/** Shared root/child scrolling. View state never changes timeline or execution state. */
	let { scopeKey, testId = 'transcript-scroll', oldestKey = '', hasOlder = false, loadingOlder = false, onOlder, children }: {
		scopeKey: string; testId?: string; oldestKey?: string; hasOlder?: boolean; loadingOlder?: boolean;
		onOlder?: () => Promise<void>; children: Snippet;
	} = $props();
	let scroller = $state<HTMLDivElement>();
	let content = $state<HTMLDivElement>();
	let away = $state(false);
	let following = true;
	let currentScope = '';
	let restoring = false;
	let generation = 0;
	let lastTop = 0;
	let previousOldest = '';
	let paging = false;
	const positions = new Map<string, { top: number; following: boolean }>();

	function remember(): void {
		if (scroller && currentScope) positions.set(currentScope, { top: scroller.scrollTop, following });
	}
	function readPosition(): void {
		if (!scroller || restoring) return;
		away = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop > 4;
		if (!away) following = true;
		else if (scroller.scrollTop < lastTop - 1) following = false;
		lastTop = scroller.scrollTop;
		remember();
		void loadNearTop();
	}
	async function loadNearTop(): Promise<void> {
		if (!scroller || restoring || paging || loadingOlder || !hasOlder || !onOlder || scroller.scrollTop > 100) return;
		if (following && scroller.scrollHeight > scroller.clientHeight + 4) return;
		paging = true;
		try { await onOlder(); } finally { paging = false; }
	}
	function followBottom(): void {
		if (!scroller || restoring) return;
		if (following) scroller.scrollTop = scroller.scrollHeight;
		readPosition();
	}
	function jumpToBottom(): void {
		following = true;
		followBottom();
	}

	/** Reveal an existing receipt, including cards inside routine disclosures. */
	export function reveal(messageId: string, executionId?: string | null): boolean {
		if (!scroller) return false;
		const candidates = [...scroller.querySelectorAll<HTMLElement>('[data-message-id], [data-execution]')];
		const target = (executionId ? candidates.find((element) => element.dataset.execution === executionId) : null)
			?? candidates.find((element) => element.dataset.messageId === messageId);
		if (!target) return false;
		following = false;
		for (const disclosure of target.querySelectorAll<HTMLDetailsElement>('.activity-receipt')) disclosure.open = true;
		const top = target.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 12;
		scroller.scrollTop = Math.max(0, top);
		target.animate?.([{ backgroundColor: 'var(--color-accent-highlight, #eaf4ee)' }, { backgroundColor: 'transparent' }],
			{ duration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 1200 });
		readPosition();
		return true;
	}

	$effect.pre(() => {
		const next = scopeKey;
		const token = ++generation;
		untrack(() => {
			remember();
			currentScope = next;
			previousOldest = '';
			restoring = true;
			const previous = positions.get(next);
			following = previous?.following ?? true;
			away = !following;
			void tick().then(() => {
				if (!scroller || token !== generation) return;
				scroller.scrollTop = following ? scroller.scrollHeight : previous?.top ?? 0;
				lastTop = scroller.scrollTop;
				restoring = false;
				readPosition();
			});
		});
	});
	$effect.pre(() => {
		const first = oldestKey;
		untrack(() => {
			const previous = previousOldest;
			previousOldest = first;
			if (!previous || first === previous || !scroller || restoring || following) return;
			const container = scroller;
			const token = generation;
			const boundary = container.getBoundingClientRect().top;
			const anchor = [...container.querySelectorAll<HTMLElement>('[data-message-id]')].find((element) => element.getBoundingClientRect().bottom > boundary);
			if (!anchor) return;
			const offset = anchor.getBoundingClientRect().top;
			void tick().then(() => {
				if (token !== generation || !anchor.isConnected || following) return;
				container.scrollTop += anchor.getBoundingClientRect().top - offset;
				lastTop = container.scrollTop;
				readPosition();
			});
		});
	});
	$effect(() => {
		void hasOlder; void loadingOlder;
		untrack(() => { void tick().then(loadNearTop); });
	});
	$effect(() => {
		if (!content || !scroller || typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(followBottom);
		observer.observe(content);
		observer.observe(scroller);
		return () => observer.disconnect();
	});
</script>

<div class="transcript-viewport">
	<div bind:this={scroller} class="transcript-scroll" data-testid={testId} onscroll={readPosition}>
		<div bind:this={content} class="transcript-content">{@render children()}</div>
	</div>
	{#if away}
		<button type="button" class="jump-bottom" onclick={jumpToBottom} aria-label="Jump to bottom" title="Jump to bottom" data-testid="jump-to-bottom">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M12 5v14m-6-6 6 6 6-6" stroke-linecap="round" stroke-linejoin="round" /></svg>
		</button>
	{/if}
</div>

<style>
	.transcript-viewport{position:relative;display:flex;flex:1;min-height:0;min-width:0}
	.transcript-scroll{flex:1;min-height:0;min-width:0;overflow-y:auto;overflow-anchor:none}
	.transcript-content{padding:12px;min-width:0;display:flow-root}
	.jump-bottom{position:absolute;bottom:12px;left:50%;transform:translateX(-50%);display:flex;align-items:center;justify-content:center;width:32px;height:32px;border:1px solid var(--color-surface-600);border-radius:50%;background:var(--color-surface-900);color:var(--color-slate-200);box-shadow:0 2px 8px rgb(0 0 0 / .06);cursor:pointer}
	.jump-bottom:hover{border-color:var(--color-accent);color:var(--color-accent)}
	.jump-bottom svg{width:16px;height:16px}
	@media(pointer:coarse){.jump-bottom{width:44px;height:44px}}
</style>
