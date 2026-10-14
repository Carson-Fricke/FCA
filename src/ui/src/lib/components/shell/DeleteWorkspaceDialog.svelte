<script lang="ts">
    import { deleteWorkspace } from '$lib/api';
    import { forgetWorkspace } from '$lib/workspace.svelte';
    import type { WorkspaceSummary } from '$lib/types';
    import Dialog from './Dialog.svelte';

    let { workspace, onDeleted, onClose }: {
        workspace: WorkspaceSummary;
        onDeleted: () => void;
        onClose: () => void;
    } = $props();
    let confirmation = $state('');
    let busy = $state(false);
    let error = $state('');

    async function submit(): Promise<void> {
        if (busy || confirmation !== workspace.name) return;
        busy = true;
        error = '';
        try {
            await deleteWorkspace(workspace.id);
        } catch (failure) {
            error = failure instanceof Error ? failure.message : String(failure);
            busy = false;
            return;
        }
        forgetWorkspace(workspace.id);
        onDeleted();
    }
</script>

<Dialog title="Delete workspace" onClose={() => { if (!busy) onClose(); }}>
    <form class="space-y-4 p-4" onsubmit={(event) => { event.preventDefault(); void submit(); }}>
        <p class="text-sm text-slate-700">
            Delete <strong>{workspace.name}</strong> and all its conversations, chats, files,
            strategies, results, and other stored data? The workspace will become unavailable
            immediately. Stored data may be permanently removed later. This cannot be undone.
        </p>
        <label class="block">
            <span class="mb-1 block text-xs text-slate-500">Type {workspace.name} to confirm</span>
            <input class="w-full rounded-lg border border-surface-600 bg-surface-950 px-3 py-2 text-sm text-slate-800"
                bind:value={confirmation} disabled={busy} autocomplete="off" data-testid="workspace-delete-confirmation" />
        </label>
        {#if error}<p role="alert" class="text-sm text-loss">{error}</p>{/if}
        <div class="flex justify-end gap-2">
            <button type="button" class="rounded-lg border border-surface-600 px-3 py-1.5 text-sm text-slate-700"
                disabled={busy} onclick={onClose}>Cancel</button>
            <button type="submit" class="rounded-lg border border-loss/40 bg-loss/10 px-3 py-1.5 text-sm font-semibold text-loss disabled:opacity-40"
                disabled={busy || confirmation !== workspace.name} data-testid="workspace-delete-submit">
                {busy ? 'Deleting…' : 'Delete workspace'}
            </button>
        </div>
    </form>
</Dialog>
