import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import * as api from '$lib/api';
import backendTree from './backend-tree.json';
import { deriveSubAgentTree } from '$lib/subagent';
import type { ExecutionEvent } from '$lib/types';
import backendMessage from './backend-message.json';
import backendThread from './backend-thread.json';
const node = backendTree.items[0];

describe('backend base classification', () => {
	it('recognizes loopback bases only', () => {
		for (const base of ['http://127.0.0.1:8000', 'http://localhost:8000', 'http://[::1]:8000']) expect(api.isLoopbackBase(base)).toBe(true);
		for (const base of ['', '127.0.0.1', 'https://thesis.example.com']) expect(api.isLoopbackBase(base)).toBe(false);
	});
	it('uses the same-origin API prefix without a baked browser credential', () => {
		expect(api.SAME_ORIGIN_API_BASE).toBe('/api');
		expect(api.apiBaseUrl).toBe('/api');
		expect('apiBearerToken' in api).toBe(false);
	});
});

describe('timeline page transport', () => {
	let realApi: typeof api;
	beforeAll(async () => {
		vi.stubEnv('PUBLIC_THESIS_API_BASE', '/api');
		vi.resetModules();
		realApi = await import('$lib/api');
	});
	afterAll(() => vi.unstubAllEnvs());
	it('preserves durable node attempt and freshness fields from the tree API', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ chatId: 'chat', roots: [node], items: [node] })));
		try { const { children, ...raw } = node; expect((await realApi.listChatSubAgents('ws', 'chat'))[0]).toMatchObject(raw); }
		finally { fetch.mockRestore(); }
	});
	it('leaves omitted sandbox context unknown rather than fabricating production data', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ chatId: 'chat', roots: [node], items: [node] })));
		try { expect((await realApi.listChatSubAgents('ws', 'chat'))[0].context).toBeNull(); }
		finally { fetch.mockRestore(); }
	});
	it('projects reopened backend tree payloads over completed prior-attempt history without dropping metadata', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(backendTree)));
		try {
			const known = await realApi.listChatSubAgents('alpha-lab', 'chat');
			const old: ExecutionEvent = { eventId: 'completed', eventType: 'sub_agent.completed', executionId: 'exec',
				workspaceId: 'alpha-lab', cursor: 3, occurredAt: '2026-01-01T00:00:03Z', actorId: 'agent', actorScope: 'assistant',
				correlationId: 'old', intentEpoch: 0, lateResult: false, payload: { subAgentId: 'node', phaseAttemptId: 'attempt-a' } };
			const projected = deriveSubAgentTree([old], known)[0].node;
			expect(projected).toMatchObject({ status: 'running', acceptsInput: true, phaseAttemptId: 'attempt-b',
				kind: 'conversation', phaseExecutor: true, completionMode: 'explicit_completion',
				allowedTools: ['validate_study'], delegatedBudget: 20, result: null, context: null });
		} finally { fetch.mockRestore(); }
	});
	it('rejects partial production trees rather than manufacturing defaults', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ chatId: 'chat', roots: [], items: [{ subAgentId: 'node' }] })));
		try { await expect(realApi.listChatSubAgents('alpha-lab', 'chat')).rejects.toMatchObject({ name: 'WireDecodeError' }); }
		finally { fetch.mockRestore(); }
	});
	it('validates the persisted node-message record and separates receipt-only attribution', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...backendMessage,
			conversation_id: 'node', parent_conversation_id: backendMessage.chat_id,
			data: { ...backendMessage.data, subAgentId: 'node' },
			subAgentId: 'node', operatorPolicy: { allowed: true, actorId: 'operator', actorKind: 'human_operator',
				allowedActorKinds: ['human_operator'], policy: 'human_only', role: 'researcher', nodeId: 'node' } })));
		try {
			const message = await realApi.postNodeMessage('workspace', backendMessage.chat_id, { kind: 'sub_agent', id: 'node' }, { body: 'feedback' });
			expect(message.data).toMatchObject({ activityKind: 'model_output', subAgentId: 'node' });
			expect(message.tool_result).toEqual(backendMessage.tool_result);
			expect('operatorPolicy' in message).toBe(false);
		} finally { fetch.mockRestore(); }
	});
	it('validates list/create thread records and projects nested links without dropping consumed positions', async () => {
		const raw = { ...backendThread, links: { ...backendThread.links, investigation_ids: ['investigation'] }, input_consumed_positions: { root: 3 } };
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify({ items: [raw] })))
			.mockResolvedValueOnce(new Response(JSON.stringify(raw)));
		try {
			const listed = (await realApi.listChats('workspace'))[0];
			const created = await realApi.createChat('workspace', { title: raw.title });
			for (const thread of [listed, created]) {
				expect(thread).toMatchObject({ kind: 'investigation', investigation_ids: ['investigation'],
					links: raw.links, input_consumed_positions: { root: 3 }, content_hash: raw.content_hash });
			}
		} finally { fetch.mockRestore(); }
	});
	it('rejects malformed full receipts rather than validating only the message portion', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...backendMessage,
			subAgentId: 'node', operatorPolicy: { allowed: true } })));
		try { await expect(realApi.postNodeMessage('workspace', backendMessage.chat_id, { kind: 'sub_agent', id: 'node' }, { body: 'feedback' })).rejects.toMatchObject({ schemaName: 'NodeInputReceiptWire' }); }
		finally { fetch.mockRestore(); }
	});
	it('validates successful production timeline pages through their own schema', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [{
			id: backendMessage.message_id, chatId: backendMessage.chat_id, workspaceId: backendMessage.workspace_id,
			kind: 'message', seq: 1, ts: backendMessage.created_at, payload: backendMessage
		}], nextBefore: null, after: 1 })));
		try {
			const page = await realApi.listTimelinePage('workspace', backendMessage.chat_id, null, null);
			expect(page.items[0].payload).toEqual(backendMessage);
			expect(page.after).toBe(1);
		}
		finally { fetch.mockRestore(); }
	});
	it('retains root/thread filter and exclusive cursor without implementing pagination in the client', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [], nextBefore: null, after: 0 })));
		try {
			await realApi.listTimelinePage('ws', 'chat', 'agent/a', 101);
			const query = new URL(String(fetch.mock.calls[0][0]), 'http://test').searchParams;
			expect(query.get('thread')).toBe('agent/a');
			expect(query.get('before')).toBe('101');
			expect(query.get('limit')).toBe('1000');
			expect(query.has('after')).toBe(false);
			expect(fetch).toHaveBeenCalledTimes(1);
		} finally { fetch.mockRestore(); }
	});
	it('aborts a stalled page after 30 seconds and releases its timer', async () => {
		vi.useFakeTimers();
		const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(() => {}));
		try {
			const pending = realApi.listTimelinePage('ws', 'chat', 'root', null);
			const rejected = expect(pending).rejects.toThrow('timed out after 30 seconds');
			await vi.advanceTimersByTimeAsync(30_000);
			await rejected;
			expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
			expect(vi.getTimerCount()).toBe(0);
		} finally { fetch.mockRestore(); vi.useRealTimers(); }
	});
	it('rejects immediately when scope cancellation interrupts an unresponsive fetch', async () => {
		const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(() => {}));
		try {
			const request = new AbortController();
			const pending = realApi.listTimelinePage('ws', 'chat', 'node', null, request.signal);
			const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
			request.abort();
			await rejected;
			expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
		} finally { fetch.mockRestore(); }
	});
});
