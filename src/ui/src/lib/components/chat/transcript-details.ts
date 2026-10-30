/** Scoped evidence loading; presentation never advances an inbox or stream cursor. */
import { getContext, setContext } from 'svelte';

const key = Symbol('transcript-details');
export type TranscriptDetails = (sequence: number) => Promise<void>;
export function setTranscriptDetails(load: TranscriptDetails): void { setContext(key, load); }
export function getTranscriptDetails(): TranscriptDetails | undefined { return getContext(key); }
