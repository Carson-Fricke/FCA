<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';

	// /w/[workspace] has no page of its own; the workspace layout renders
	// only under its child sections, so land on the overview.
	const workspaceId = $derived(page.params.workspace);
	if (workspaceId) {
		goto(`/w/${workspaceId}/overview`, { replaceState: true });
	}
</script>