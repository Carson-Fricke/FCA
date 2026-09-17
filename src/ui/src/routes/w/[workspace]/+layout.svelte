<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import BackendSettingsDialog from '$lib/components/shell/BackendSettingsDialog.svelte';
	import CommandPalette from '$lib/components/shell/CommandPalette.svelte';
	import CopilotPanel from '$lib/components/shell/CopilotPanel.svelte';
	import CreateWorkspaceDialog from '$lib/components/shell/CreateWorkspaceDialog.svelte';
	import Sidebar from '$lib/components/shell/Sidebar.svelte';
	import TopBar from '$lib/components/shell/TopBar.svelte';
import ToastStack from '$lib/components/common/ToastStack.svelte';
import DataModeBadge from '$lib/components/shell/DataModeBadge.svelte';
	import { createWorkspace, isLocalHosting, listStrategies, listWidgets } from '$lib/api';
	import {
		COPILOT_MAX_FRACTION,
		COPILOT_MIN_WIDTH,
		notify,
		setCopilotWidth,
		setLastWorkspace,
		ui
	} from '$lib/workspace.svelte';
	import type { Strategy, WidgetDefinition } from '$lib/types';
	import type { LayoutData } from './$types';

	let { data, children }: { data: LayoutData; children: any } = $props();

	const workspaceId = $derived(data.workspaceId);
	const pageLabel = $derived(
		page.url.pathname.replace(new RegExp(`^/w/${workspaceId}/?`), '').split('/')[0] || 'overview'
	);
	const isChats = $derived(pageLabel === 'conversations');
	const copilotVisible = $derived(!isChats && ui.copilotOpen);

	$effect(() => {
		// The full-page conversations view hides the copilot pane. Clear the
		// open state there so leaving the page defaults the pane back to closed
		// instead of resurrecting a stale open state.
		if (isChats) ui.copilotOpen = false;
	});

	let paletteOpen = $state(false);
	let createOpen = $state(false);
	let settingsOpen = $state(false);
	let strategies = $state<Strategy[]>([]);
	let widgets = $state<WidgetDefinition[]>([]);

	/**
	 * The copilot pane is resized by directly mutating the wrapper's inline
	 * width during the drag. Committing to reactive state on every pointermove
	 * would re-render the whole panel dozens of times per second, so only the
	 * final width is written back to `ui.copilotWidth` (and localStorage).
	 */
	let copilotWrapper = $state<HTMLDivElement | null>(null);
	let resizingCopilot = $state(false);
	let pendingCopilotWidth = 0;

	function copilotMaxWidth(): number {
		return window.innerWidth * COPILOT_MAX_FRACTION;
	}

	function startCopilotResize(event: PointerEvent): void {
		if (!copilotWrapper) return;
		event.preventDefault();
		pendingCopilotWidth = copilotWrapper.getBoundingClientRect().width;
		resizingCopilot = true;
		window.addEventListener('pointermove', onCopilotResizeMove);
		window.addEventListener('pointerup', endCopilotResize);
		window.addEventListener('pointercancel', endCopilotResize);
	}

	function onCopilotResizeMove(event: PointerEvent): void {
		if (!resizingCopilot || !copilotWrapper) return;
		pendingCopilotWidth = window.innerWidth - event.clientX;
		const preview = Math.min(
			Math.max(pendingCopilotWidth, COPILOT_MIN_WIDTH),
			copilotMaxWidth()
		);
		copilotWrapper.style.width = `${preview}px`;
	}

	function endCopilotResize(): void {
		if (!resizingCopilot) return;
		resizingCopilot = false;
		window.removeEventListener('pointermove', onCopilotResizeMove);
		window.removeEventListener('pointerup', endCopilotResize);
		window.removeEventListener('pointercancel', endCopilotResize);
		if (!copilotWrapper) return;
		// Trying to go below the minimum width minimizes the pane instead.
		if (pendingCopilotWidth < COPILOT_MIN_WIDTH) {
			ui.copilotOpen = false;
			return;
		}
		const settled = Math.min(pendingCopilotWidth, copilotMaxWidth());
		copilotWrapper.style.width = `${settled}px`;
		setCopilotWidth(settled);
	}

	function clampCopilotWidth(): void {
		if (!copilotWrapper) return;
		const max = copilotMaxWidth();
		if (ui.copilotWidth > max) {
			copilotWrapper.style.width = `${max}px`;
			setCopilotWidth(max);
		}
	}

	$effect(() => {
		return () => {
			window.removeEventListener('pointermove', onCopilotResizeMove);
			window.removeEventListener('pointerup', endCopilotResize);
			window.removeEventListener('pointercancel', endCopilotResize);
		};
	});

	$effect(() => {
		setLastWorkspace(workspaceId);
	});

	$effect(() => {
		let cancelled = false;
		listStrategies(workspaceId).then((items) => {
			if (!cancelled) strategies = items;
		});
		listWidgets(workspaceId).then((items) => {
			if (!cancelled) widgets = items;
		});
		return () => {
			cancelled = true;
		};
	});

	function onKeydown(event: KeyboardEvent): void {
		if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
			event.preventDefault();
			paletteOpen = !paletteOpen;
		}
		if (event.key === 'Escape') {
			paletteOpen = false;
			createOpen = false;
			settingsOpen = false;
		}
	}

	function switchWorkspace(id: string): void {
		goto(`/w/${id}/overview`);
	}

	function switchToConversations(): void {
		goto(`/w/${workspaceId}/conversations`);
	}
</script>

<svelte:window onkeydown={onKeydown} onresize={clampCopilotWidth} />

<svelte:head><title>Thesis — {data.snapshot.overview.workspace.name}</title></svelte:head>

<div class="grid-bg flex h-screen overflow-hidden bg-surface-950">
	<Sidebar
		snapshot={data.snapshot}
		{workspaceId}
		page={pageLabel}
		{widgets}
		onSearch={() => (paletteOpen = true)}
		onSwitch={switchWorkspace}
		onCreateWorkspace={() => (createOpen = true)}
		onSettings={() => (settingsOpen = true)}
		canConfigureBackend={isLocalHosting}
	/>

	<div class="flex min-w-0 flex-1 flex-col">
		<TopBar
			{workspaceId}
			workspaceName={data.snapshot.overview.workspace.name}
			page={pageLabel}
			activity={data.snapshot.overview.activity}
			copilotHidden={isChats}
			copilotVisible={copilotVisible}
			onToggleCopilot={() => (ui.copilotOpen = !ui.copilotOpen)}
		/>
		<main class="min-h-0 flex-1 overflow-y-auto p-5" id="main-content">
			{@render children()}
		</main>
	</div>

	{#if copilotVisible}
		<div
			class="relative flex shrink-0 border-l border-surface-700 bg-surface-900 {resizingCopilot
				? 'select-none'
				: 'transition-[width] duration-200 ease-out'}"
			style="width: {ui.copilotWidth}px"
			bind:this={copilotWrapper}
			data-testid="copilot-aside"
		>
			<div
				class="group absolute inset-y-0 -left-1 z-40 flex w-2 cursor-col-resize touch-none items-center justify-center"
				role="separator"
				aria-orientation="vertical"
				aria-label="Resize copilot"
				aria-valuemin={COPILOT_MIN_WIDTH}
				aria-valuenow={ui.copilotWidth}
				onpointerdown={startCopilotResize}
				data-testid="copilot-resize-handle"
			>
				<span
					class="h-8 w-1 rounded-full bg-surface-700 transition group-hover:bg-accent/60"
				></span>
			</div>
			<CopilotPanel
				{workspaceId}
				workspaceName={data.snapshot.overview.workspace.name}
				onExpand={switchToConversations}
				onClose={() => (ui.copilotOpen = false)}
			/>
		</div>
	{/if}
</div>

{#if paletteOpen}
	<CommandPalette
		{workspaceId}
		{strategies}
		{widgets}
		onClose={() => (paletteOpen = false)}
	/>
{/if}

{#if createOpen}
	<CreateWorkspaceDialog
		onClose={() => (createOpen = false)}
		onCreate={async (input) => {
			createOpen = false;
			try {
				const workspace = await createWorkspace(input);
				notify(`Workspace “${workspace.name}” created`);
				await goto(`/w/${workspace.id}/overview`);
			} catch (error) {
				notify(`Workspace creation failed: ${error instanceof Error ? error.message : error}`, true);
			}
		}}
	/>
{/if}

{#if settingsOpen}
	<BackendSettingsDialog onClose={() => (settingsOpen = false)} />
{/if}

<DataModeBadge />

<ToastStack />
