import { describe, expect, it, vi } from 'vitest';
import { ChatHostController } from '$lib/chat-host.svelte';
import { conversationNavigation } from '$lib/chat-navigation';
import { createChat, deleteChat, fixtureMode, getSnapshot, listTimelinePage, postMessage, postNodeMessage } from '$lib/api';
import { fetchJson, ThesisApiError } from '$lib/transport';

describe('exported offline API and shared chat host', () => {
	it('uses seeded messages, execution events and records in one ordered timeline', async () => {
		expect(fixtureMode).toBe(true);
		const page = await listTimelinePage('alpha-lab', 'chat-1', null, null);
		expect(page.items.some((item) => item.kind === 'message')).toBe(true);
		expect(page.items.some((item) => item.kind === 'event')).toBe(true);
		expect(page.items.some((item) => item.kind === 'execution')).toBe(true);
		expect(new Set(page.items.map((item) => item.seq)).size).toBe(page.items.length);
	});

	it.each(['page', 'copilot'])('keeps %s sends visible live and on re-selection without duplication', async (presentation) => {
		let url = new URL('http://localhost/w/offline/conversations');
		const host = new ChatHostController(presentation === 'page'
			? conversationNavigation(() => url, (next) => { url = next; }) : undefined);
		await host.open(`offline-${presentation}`);
		host.newConversation();
		await host.send({ body: `first ${presentation}`, attachmentIds: [], steeringIntent: null });
		const id = host.chatId!;
		await vi.waitFor(() => expect(host.timeline.messages.map((message) => message.body)).toEqual([`first ${presentation}`]));
		await host.send({ body: 'second', attachmentIds: [], steeringIntent: null });
		expect(host.timeline.messages).toHaveLength(2);
		host.select(id);
		await vi.waitFor(() => expect(host.timeline.messages).toHaveLength(2));
		if (presentation === 'page') expect(url.searchParams.get('chat')).toBe(id);
		expect(await host.remove(id)).toBe(true);
		expect(host.chatId).toBeNull();
		expect((await listTimelinePage(`offline-${presentation}`, id, null, null)).items).toEqual([]);
		host.stop();
	});

	it('appends root and node mutations to the same storage, filtered by thread', async () => {
		const chat = await createChat('offline-node');
		await postMessage('offline-node', chat.chat_id, { body: 'root' });
		await postNodeMessage('offline-node', chat.chat_id, { kind: 'sub_agent', id: 'node' }, { body: 'midturn feedback' });
		expect((await listTimelinePage('offline-node', chat.chat_id, 'root', null)).items.flatMap((item) => item.kind === 'message' ? [item.payload.body] : [])).toEqual(['root']);
		expect((await listTimelinePage('offline-node', chat.chat_id, 'node', null)).items.flatMap((item) => item.kind === 'message' ? [item.payload.body] : [])).toEqual(['midturn feedback']);
		await deleteChat('offline-node', chat.chat_id);
	});
	it('updates seeded workspace chat counts after create/delete', async () => {
		const before = (await getSnapshot('alpha-lab')).counts.chats;
		const chat = await createChat('alpha-lab');
		expect((await getSnapshot('alpha-lab')).counts.chats).toBe(before + 1);
		await deleteChat('alpha-lab', chat.chat_id);
		expect((await getSnapshot('alpha-lab')).counts.chats).toBe(before);
	});
	it('normalizes fixture steering through the same response adapter as production', async () => {
		const result = await postMessage('alpha-lab', 'chat-1', { body: 'focus fixture', steeringIntent: 'focus fixture' });
		expect(result.percolated).toEqual([{ execution_id: 'exec-1', intent_epoch: 1, steering_intent: 'focus fixture' }]);
	});

	it('drops scope and hash on selection while preserving the initial deep link', async () => {
		let url = new URL('http://localhost/w/alpha-lab/conversations?chat=chat-1&scope=conversation:chat-1/sub_agent:node#node');
		const host = new ChatHostController(conversationNavigation(() => url, (next) => { url = next; }));
		await host.open('alpha-lab');
		expect(host.scopeGrant).toContain('sub_agent:node');
		expect(host.hashGrant).toBe('#node');
		host.select('chat-2');
		expect(url.searchParams.has('scope')).toBe(false);
		expect(url.hash).toBe('');
		expect(host.scopeGrant).toBeNull();
		host.stop();
	});

	it('does not replace a draft when a delayed chat list arrives', async () => {
		const host = new ChatHostController();
		const pending = host.open('alpha-lab');
		host.newConversation();
		await pending;
		expect(host.drafting).toBe(true);
		expect(host.chatId).toBeNull();
		host.stop();
	});
});

describe('structured HTTP errors', () => {
	it('decodes the canonical backend error envelope', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
			error: { code: 'agent_closed', message: 'The agent is closed', details: { agentId: 'node' } }
		}), { status: 409 }));
		try { await expect(fetchJson('/api', '/messages')).rejects.toMatchObject({ status: 409, code: 'agent_closed', message: 'The agent is closed', details: { agentId: 'node' } }); }
		finally { fetch.mockRestore(); }
	});
	it('reports malformed successful JSON explicitly', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('<html>not JSON</html>'));
		try { await expect(fetchJson('/api', '/messages')).rejects.toMatchObject({ name: 'WireDecodeError', path: '/messages' }); }
		finally { fetch.mockRestore(); }
	});
	it.each([409, 422, 503])('preserves status %s, route, server code, message and details', async (status) => {
		const details = { node: 'closed' };
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
			detail: { code: 'not_accepting_input', message: 'This agent has stopped', details }
		}), { status }));
		try {
			await expect(fetchJson('/api', '/messages')).rejects.toMatchObject({
				name: 'ThesisApiError', status, path: '/messages', code: 'not_accepting_input',
				message: 'This agent has stopped', details
			});
		} finally { fetch.mockRestore(); }
	});
	it('does not show proxy HTML as an operator error', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('<html>proxy error</html>', { status: 502 }));
		try { await expect(fetchJson('/api', '/messages')).rejects.toEqual(new ThesisApiError(502, '/messages')); }
		finally { fetch.mockRestore(); }
	});
});
