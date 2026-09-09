<script lang="ts">
	import { goto } from '$app/navigation';

	// Landing route: the picker resolves the remembered workspace with an
	// existence check, so a stale id can never land on the error page here.
	goto('/w', { replaceState: true });
</script>
