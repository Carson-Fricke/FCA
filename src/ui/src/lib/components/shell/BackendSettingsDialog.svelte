<script lang="ts">
	import Dialog from './Dialog.svelte';
	import { azureBaseUrl, backendKind, backendOptions, isLocalHosting, setBackend } from '$lib/api';
	import type { BackendKind } from '$lib/types';

	/** Backend chooser. Only mounted when the UI is served locally. */
	let { onClose }: { onClose: () => void } = $props();

	const options = backendOptions();
	let selected = $state<BackendKind>(backendKind);
	let azureBase = $state(azureBaseUrl);
	let applying = $state(false);

	const selectable = $derived(
		selected === 'azure'
			? azureBase.trim() !== ''
			: (options.find((option) => option.kind === selected)?.available ?? false)
	);

	const selectedLabel = $derived(
		options.find((option) => option.kind === selected)?.label.toLowerCase() ?? ''
	);

	function apply(): void {
		if (!selectable) return;
		applying = true;
		setBackend(selected, selected === 'azure' ? azureBase : undefined);
	}
</script>

<Dialog title="Backend settings" {onClose}>
	<div class="space-y-3 p-4" data-testid="backend-settings">
		<p class="text-xs text-slate-500">
			Choose where this UI reads and writes. Switching reloads the page.
			{#if !isLocalHosting}
				Backend selection is only available when the UI is hosted locally.
			{/if}
		</p>

		<ul class="space-y-2">
			{#each options as option (option.kind)}
				<li>
					<label
						class="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition
							{selected === option.kind
							? 'border-accent/50 bg-accent/5'
							: 'border-surface-700 hover:border-surface-600'}
							{option.available ? '' : 'cursor-not-allowed opacity-50'}"
					>
						<input
							type="radio"
							name="backend-kind"
							class="mt-0.5 accent-[var(--color-accent)]"
							value={option.kind}
							checked={selected === option.kind}
							disabled={!option.available}
							onchange={() => (selected = option.kind)}
							data-testid="backend-option-{option.kind}"
						/>
						<span class="min-w-0">
							<span class="block text-sm font-medium text-slate-800">{option.label}</span>
							<span class="block text-xs text-slate-500">{option.detail}</span>
							{#if option.base}
								<span class="mt-1 block truncate font-mono text-[10px] text-slate-500">
									{option.base}
								</span>
							{/if}
						</span>
					</label>
				</li>
			{/each}
		</ul>

		{#if selected === 'azure'}
			<label class="block">
				<span class="mb-1 block text-xs font-medium text-slate-700">Azure API base URL</span>
				<input
					type="url"
					class="w-full rounded-lg border border-surface-600 bg-surface-950 px-3 py-2 font-mono text-xs text-slate-800 outline-none placeholder:text-slate-500 focus:border-accent/50"
					placeholder="https://thesis.example.com"
					bind:value={azureBase}
					data-testid="backend-azure-base"
				/>
				<span class="mt-1 block text-[11px] text-slate-500">
					The bearer token still comes from
					<code>PUBLIC_THESIS_API_BEARER_TOKEN</code> at build time.
				</span>
			</label>
		{/if}

		<div class="flex items-center justify-end gap-2 border-t border-surface-700 pt-3">
			<button
				type="button"
				class="rounded-lg border border-surface-600 px-3 py-1.5 text-sm text-slate-600 transition hover:text-slate-800"
				onclick={onClose}
			>
				Cancel
			</button>
			<button
				type="button"
				class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-sm font-semibold text-accent transition hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
				disabled={!selectable || applying}
				onclick={apply}
				data-testid="backend-apply"
			>
				Use {selectedLabel}
			</button>
		</div>
	</div>
</Dialog>
