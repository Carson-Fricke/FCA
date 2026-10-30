<script lang="ts">
 import { getTranscriptDetails } from './transcript-details';
 let { sequence }: { sequence: number } = $props();
 const load = getTranscriptDetails();
 let loading = $state(false);
 let error = $state('');
 async function expand(): Promise<void> {
  if (!load || loading) return;
  loading = true; error = '';
  try { await load(sequence); }
  catch (failure) { error = failure instanceof Error ? failure.message : 'Could not load message'; }
  finally { loading = false; }
 }
</script>
<button type="button" class="mt-2 text-xs text-accent" disabled={loading} onclick={expand} data-testid="message-details">{loading ? 'Loading full message…' : 'Show full message'}</button>
{#if error}<p role="status" class="text-xs text-warn">{error}</p>{/if}
