<script lang="ts">
	import { goto } from '$app/navigation';
	import { ui } from '$lib/workspace.svelte';

	// Landing route: honor the remembered workspace, otherwise the picker.
	if (ui.lastWorkspace) {
		goto(`/w/${ui.lastWorkspace}`, { replaceState: true });
	} else {
		goto('/w', { replaceState: true });
	}
</script>
