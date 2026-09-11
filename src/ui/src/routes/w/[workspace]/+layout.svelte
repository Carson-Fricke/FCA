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
import { notify, setLastWorkspace, ui } from '$lib/workspace.svelte';
	import type { Strategy, WidgetDefinition } from '$lib/types';
	import type { LayoutData } from './$types';

	let { data, children }: { data: LayoutData; children: any } = $props();

	const workspaceId = $derived(data.workspaceId);
	const pageLabel = $derived(
		page.url.pathname.replace(new RegExp(`^/w/${workspaceId}/?`), '').split('/')[0] || 'overview'
	);
	const isChats = $derived(pageLabel === 'conversations');
	const copilotVisible = $derived(!isChats && ui.copilotOpen);

	let paletteOpen = $state(false);
	let createOpen = $state(false);
	let settingsOpen = $state(false);
	let strategies = $state<Strategy[]>([]);
	let widgets = $state<WidgetDefinition[]>([]);

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

<svelte:window onkeydown={onKeydown} />

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
		<div class="relative flex">
			<CopilotPanel
				{workspaceId}
				workspaceName={data.snapshot.overview.workspace.name}
				onExpand={switchToConversations}
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
