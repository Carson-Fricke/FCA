import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { TimelineItem } from '$lib/types';
import type { TimelinePage } from '$lib/api';
import type { TimelineStreamOptions } from '$lib/sse';
import { timelineFixture, transcriptFixture } from './timeline-fixture';
vi.mock('$lib/api', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/api')>(), fixtureMode: false,
	listWorkflowMetadata: vi.fn(async () => ({ items: [], after: 0 })),
	listTimelinePage: vi.fn(async () => ({ items: [], nextBefore: null, after: 0 }))
}));
const streams = vi.hoisted(() => [] as Array<(item: TimelineItem) => void>);
const streamOptions = vi.hoisted(() => [] as TimelineStreamOptions[]);
vi.mock('$lib/sse', () => ({ TimelineStream: class {
	constructor(options: TimelineStreamOptions) { streamOptions.push(options); }
	async start(callback: (item: TimelineItem) => void) { streams.push(callback); }
	stop() {}
} }));
import { TimelineController } from '$lib/timeline.svelte';
import { listTimelinePage } from '$lib/api';
import { buildNodeEntries, threadActivity, toChatMessage } from '$lib/transcript';
import { decodeWire } from '$lib/wire';
import type { TimelineMessageWire } from '$lib/generated';
import { fixtureTimeline } from '$lib/fixture-timeline';
import { fixtures } from '$lib/fixtures';

const item = (seq: number, threadId = 'root'): TimelineMessageWire => ({ id: `item-${seq}`, seq, threadId,
	kind: 'message', chatId: 'chat', workspaceId: 'ws', ts: '2026-01-01', payload: decodeWire('ChatMessageWire', {
        ...fixtures.chatMessages['chat-1'].items[0],
		message_id: `message-${seq}`, role: 'user', body: `${seq}`, created_at: '2026-01-01', data: {} }, 'test/timeline') });
const historyItem = (seq: number, threadId = 'root') => transcriptFixture([item(seq, threadId)])[0];
beforeEach(() => { vi.clearAllMocks(); streams.length = 0; streamOptions.length = 0; });

describe('one timeline authority', () => {
	it('loads the root recent page and streams without fetching older or child history', async () => {
		let finish!: (page: TimelinePage) => void;
		let finishOlder!: (page: TimelinePage) => void;
		vi.mocked(listTimelinePage).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		vi.mocked(listTimelinePage).mockImplementationOnce(() => new Promise((resolve) => { finishOlder = resolve; }));
		const timeline = new TimelineController(); timeline.start('ws', 'chat');
		expect(streams).toHaveLength(0);
		expect(timeline.connected).toBe(false);
		expect(vi.mocked(listTimelinePage).mock.calls[0].slice(0, 4)).toEqual(['ws', 'chat', 'root', null]);
		expect(timeline.initialLoading).toBe(true);
		expect(timeline.items).toEqual([]);
		finish({ items: [historyItem(2), historyItem(3)], nextBefore: 2, after: 10 });
		await vi.waitFor(() => expect(timeline.initialLoading).toBe(false));
		expect(streamOptions[0].after).toBe(10);
		expect(timeline.history.root.loading).toBe(false);
		expect(listTimelinePage).toHaveBeenCalledTimes(1);
		expect(timeline.history.root.complete).toBe(false);
		expect(timeline.connected).toBe(true);
		streams[0](item(11));
		const older = timeline.loadHistory('root', true);
		finishOlder({ items: [historyItem(1), historyItem(2)], nextBefore: null, after: 11 });
		await older;
		expect(timeline.history.root.complete).toBe(true);
		expect(timeline.items.map((entry) => entry.seq)).toEqual([1, 2, 3, 11]);
		expect(streamOptions).toHaveLength(1);
		expect(streamOptions[0].after).toBe(10);
		timeline.stop();
	});
	it('does not stream on initial history failure and opens from the retry watermark', async () => {
		vi.mocked(listTimelinePage).mockRejectedValueOnce(new Error('History unavailable'));
		const timeline = new TimelineController(); timeline.start('ws', 'chat');
		await vi.waitFor(() => expect(timeline.history.root?.error).toBe('History unavailable'));
		expect(timeline.initialLoading).toBe(false);
		expect(timeline.items).toEqual([]);
		expect(streams).toHaveLength(0);
		expect(timeline.connected).toBe(false);
		vi.mocked(listTimelinePage).mockResolvedValueOnce({ items: [], nextBefore: null, after: 17 });
		await timeline.loadHistory();
		expect(timeline.history.root.complete).toBe(true);
		expect(streamOptions[0].after).toBe(17);
		timeline.stop();
	});
	it('loads older pages only on demand while preserving live identities and seq', async () => {
		vi.mocked(listTimelinePage).mockResolvedValueOnce({ items: Array.from({ length: 1000 }, (_, i) => historyItem(i + 1201)), nextBefore: 1201, after: 2200 })
			.mockResolvedValueOnce({ items: Array.from({ length: 1000 }, (_, i) => historyItem(i + 201)), nextBefore: 201, after: 2200 })
			.mockResolvedValueOnce({ items: Array.from({ length: 200 }, (_, i) => historyItem(i + 1)), nextBefore: 1, after: 2200 })
			.mockResolvedValueOnce({ items: [], nextBefore: null, after: 2200 });
		const timeline = new TimelineController(); timeline.start('ws', 'chat');
		await vi.waitFor(() => expect(streams).toHaveLength(1));
		streams[0](item(2200));
		expect(listTimelinePage).toHaveBeenCalledTimes(1);
		await timeline.loadHistory('root', true);
		await timeline.loadHistory('root', true);
		await timeline.loadHistory('root', true);
		expect(timeline.history.root.complete).toBe(true);
		expect(timeline.items).toHaveLength(2200);
		expect(timeline.messages.map((message) => message.seq)).toEqual(Array.from({ length: 2200 }, (_, i) => i + 1));
		expect(vi.mocked(listTimelinePage).mock.calls.map((call) => call[3])).toEqual([null, 1201, 201, 1]);
		timeline.stop();
	});
	it('rejects stalled pagination, retains partial pages and retries', async () => {
		vi.mocked(listTimelinePage).mockResolvedValueOnce({ items: [historyItem(2)], nextBefore: 2, after: 2 })
			.mockResolvedValueOnce({ items: [historyItem(2)], nextBefore: 2, after: 3 });
		const timeline = new TimelineController(); timeline.start('ws', 'chat');
		await vi.waitFor(() => expect(timeline.history.root?.loaded).toBe(true));
		await timeline.loadHistory('root', true);
		expect(timeline.history.root.error).toContain('cursor did not advance');
		expect(timeline.items).toHaveLength(1);
		await timeline.loadHistory();
		expect(timeline.history['root'].complete).toBe(true);
		expect(streamOptions).toHaveLength(1);
		expect(streamOptions[0].after).toBe(2);
		timeline.stop();
	});
	it('ignores stale live callbacks and history when switching chats', async () => {
		const timeline = new TimelineController(); timeline.start('ws', 'first');
		await vi.waitFor(() => expect(streams).toHaveLength(1));
		const oldStream = streams[0];
		let finish!: (page: TimelinePage) => void;
		vi.mocked(listTimelinePage).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		timeline.start('ws', 'old');
		timeline.start('ws', 'new');
		oldStream(item(1)); finish({ items: [historyItem(2)], nextBefore: null, after: 2 });
		await Promise.resolve();
		expect(timeline.items).toEqual([]);
		timeline.stop();
	});
	it('keeps scoped history filtered without opening a second stream', async () => {
		const timeline = new TimelineController(); timeline.start('ws', 'chat');
		await vi.waitFor(() => expect(timeline.history.root.complete).toBe(true));
		vi.mocked(listTimelinePage).mockResolvedValueOnce({ items: [historyItem(5, 'node')], nextBefore: null, after: 20 });
		await timeline.loadHistory('node');
		expect(vi.mocked(listTimelinePage).mock.calls[1].slice(0, 4)).toEqual(['ws', 'chat', 'node', null]);
		expect(timeline.messagesFor('node').map((message) => message.seq)).toEqual([5]);
		expect(streamOptions).toHaveLength(1);
		timeline.stop();
	});
	it('ignores cancelled root pages and starts streaming only after a successful retry', async () => {
		let finish!: (page: TimelinePage) => void;
		vi.mocked(listTimelinePage).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		const timeline = new TimelineController(); timeline.start('ws', 'chat');
		const signal = vi.mocked(listTimelinePage).mock.calls[0][4];
		timeline.cancelHistory('root');
		expect(signal?.aborted).toBe(true);
		expect(timeline.initialLoading).toBe(false);
		await timeline.loadHistory();
		finish({ items: [historyItem(99)], nextBefore: null, after: 99 });
		await Promise.resolve();
		expect(timeline.items).toEqual([]);
		expect(streamOptions).toHaveLength(1);
		expect(streamOptions[0].after).toBe(0);
		timeline.stop();
	});
	it('hydrates event and execution records from the same all-thread REST history', async () => {
		const page = fixtureTimeline.page('alpha-lab', 'chat-1', null, null);
		expect(page.items.some((entry) => entry.kind === 'event')).toBe(true);
		expect(page.items.some((entry) => entry.kind === 'execution')).toBe(true);
		vi.mocked(listTimelinePage).mockResolvedValueOnce({ ...page, items: transcriptFixture(page.items) });
		const timeline = new TimelineController(); timeline.start('alpha-lab', 'chat-1');
		await vi.waitFor(() => expect(timeline.history.root.complete).toBe(true));
		expect(timeline.events).toHaveLength(page.items.filter((entry) => entry.kind === 'event').length);
		expect(timeline.items.filter((entry) => entry.kind === 'execution')).toHaveLength(1);
		expect(streamOptions[0].after).toBe(page.after);
		timeline.stop();
	});
	it('indexes root/scoped records and preserves seq through activity projection despite skewed timestamps', () => {
		const timeline = timelineFixture();
		timeline.merge([item(3, 'node'), item(1, 'node'), item(2)]);
		const messages = timeline.messagesFor('node');
		messages[0].created_at = '2027-01-01'; messages[1].created_at = '2025-01-01';
		expect(buildNodeEntries({ messages, events: [] }).map((entry) => entry.order)).toEqual([1, 3]);
		expect(timeline.messagesFor('root').map((message) => message.seq)).toEqual([2]);
	});
	it('derives pending requests and delivery from historical/live activity with skewed timestamps', () => {
		const timeline = new TimelineController();
		const prompt = item(5, 'node');
		const answer: TimelineMessageWire = { ...item(6, 'node'), payload: { ...item(6).payload, role: 'assistant', body: 'Done', created_at: '2024-01-01' } };
		timeline.merge([prompt]);
		expect(threadActivity(timeline.messagesFor('node'), [], false, 'message-5')).toEqual({ pending: true, queued: true });
		timeline.merge([answer]);
		expect(threadActivity(timeline.messagesFor('node'), [], false, 'message-5')).toEqual({ pending: false, queued: false });
	});
});

describe('fixture snapshot watermark', () => {
	it('retains the chat-wide watermark for filtered, empty and backward pages', () => {
		const snapshot = fixtureTimeline.page('alpha-lab', 'chat-1', null, null);
		expect(snapshot.after).toBeGreaterThan(0);
		expect(fixtureTimeline.page('alpha-lab', 'chat-1', 'missing', null)).toEqual({ items: [], nextBefore: null, after: snapshot.after });
		expect(fixtureTimeline.page('alpha-lab', 'chat-1', null, 1)).toEqual({ items: [], nextBefore: null, after: snapshot.after });
		expect(fixtureTimeline.page('ws', 'empty', null, null).after).toBe(0);
	});
	it('replays arrivals between the history snapshot and subscription, then delivers new items once', () => {
		const chatId = 'fixture-snapshot-race';
		const snapshot = fixtureTimeline.page('alpha-lab', chatId, null, null);
		const message = toChatMessage({ ...fixtures.chatMessages['chat-1'].items[0], chat_id: chatId });
		fixtureTimeline.message({ ...message, message_id: 'fixture-race-1' });
		const received: TimelineItem[] = [];
		const unsubscribe = fixtureTimeline.subscribe('alpha-lab', chatId, snapshot.after, (item) => received.push(item));
		try {
			fixtureTimeline.message({ ...message, message_id: 'fixture-race-2' });
			expect(received.map((item) => item.seq)).toEqual([1, 2]);
		} finally {
			unsubscribe();
			fixtureTimeline.remove('alpha-lab', chatId);
		}
	});
});
