<script lang="ts">
	import { money, pct, signedPct } from '$lib/format';

	/** One metric tile in a summary strip or metric card row. */
	let {
		label,
		value,
		format = 'text'
	}: {
		label: string;
		value: number | string;
		format?: 'text' | 'money' | 'pct' | 'signedPct' | 'ratio';
	} = $props();

	const text = $derived.by(() => {
		if (typeof value === 'string') return value;
		switch (format) {
			case 'money':
				return money(value);
			case 'pct':
				return pct(value);
			case 'signedPct':
				return signedPct(value);
			case 'ratio':
				return value.toFixed(2);
			default:
				return String(value);
		}
	});

	const tone = $derived.by(() => {
		if (typeof value !== 'number') return '';
		if (format === 'ratio') return '';
		if (value > 0) return 'text-profit';
		if (value < 0) return 'text-loss';
		return '';
	});
</script>

<div
	class="rounded-xl border border-white/5 bg-surface-850/60 px-4 py-3"
	data-testid="metric-card"
>
	<div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">{label}</div>
	<div class="mt-1 font-mono text-lg font-medium tabular-nums {tone}">{text}</div>
</div>
