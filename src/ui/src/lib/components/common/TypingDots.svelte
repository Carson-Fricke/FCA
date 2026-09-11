<script lang="ts">
	/** Animated ". .. ..." typing indicator. */
	let count = $state(1);

	$effect(() => {
		const timer = setInterval(() => {
			count = count >= 3 ? 1 : count + 1;
		}, 400);
		return () => clearInterval(timer);
	});
</script>

<span
	class="inline-flex items-center gap-0.5 align-middle"
	role="status"
	aria-label="Assistant is typing"
	data-testid="typing-dots"
>
	{#each [1, 2, 3] as dot (dot)}
		<span
			class="h-1 w-1 rounded-full bg-slate-400 transition-opacity duration-150 {dot <= count
				? 'opacity-100'
				: 'opacity-0'}"
			aria-hidden="true"
		></span>
	{/each}
</span>
