<script lang="ts">
	import type { ArtifactKind } from '$lib/types';

	/** One artifact-kind chip with the shared gallery icon and tone. */
	let { kind }: { kind: ArtifactKind } = $props();

	const META: Record<ArtifactKind, { label: string; icon: string; cls: string }> = {
		sketch: { label: 'Strategy sketch', icon: '◇', cls: 'border-accent/40 text-accent' },
		strategy: { label: 'Strategy', icon: '❮', cls: 'border-accent/40 text-accent' },
		study: { label: 'Study', icon: '❡', cls: 'border-warn/40 text-warn' },
		indicator: { label: 'Indicator', icon: '∿', cls: 'border-profit/40 text-profit' }
	};
</script>

<span
	class="inline-flex items-center gap-1 rounded-full border bg-surface-900 px-2 py-0.5 font-mono text-[10px] {META[
		kind
	].cls}"
	data-testid="artifact-kind"
>
	<span aria-hidden="true">{META[kind].icon}</span>
	{META[kind].label}
</span>
