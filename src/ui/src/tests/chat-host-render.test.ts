import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fixtures } from '$lib/fixtures';
import type { TimelineItemWire } from '$lib/generated';
import { decodeWire } from '$lib/wire';
import { aggregateEnvelope, aggregateMessage } from './timeline-fixture';

const live = vi.hoisted(() => {
	vi.stubEnv('PUBLIC_THESIS_API_BASE', '/api');
	return { receive: null as null | ((item: TimelineItemWire) => void), after: null as number | null };
});
vi.mock('$app/navigation', () => ({ replaceState: (url: URL) => history.replaceState({}, '', url) }));
vi.mock('$app/state', () => ({ page: { url: new URL('http://localhost/w/alpha-lab/conversations') } }));
vi.mock('$lib/sse', async (original) => ({
	...await original<typeof import('$lib/sse')>(), TimelineStream: class {
		constructor(options: { after: number }) { live.after = options.after; }
		async start(receive: (item: TimelineItemWire) => void) { live.receive = receive; }
		stop() { live.receive = null; }
	}
}));

import CopilotPanel from '$lib/components/shell/CopilotPanel.svelte';
import ConversationsPage from '../routes/w/[workspace]/conversations/+page.svelte';
import { ChatHostController } from '$lib/chat-host.svelte';
import ChatSurface from '$lib/components/chat/ChatSurface.svelte';
import { TimelineController } from '$lib/timeline.svelte';
import backendTree from './backend-tree.json';

// Layout load data now carries the workspace summary from the render gate.
const layoutData = (id: string) => ({
	workspaceId: id,
	workspace: fixtures.workspaces.items.find((workspace) => workspace.id === id)!
});

let requests: { path: string; init?: RequestInit }[];
let rejectPost: boolean;
let rejectDelete: boolean;
beforeEach(() => {
	live.receive = null;
	live.after = null;
	requests = []; rejectPost = false; rejectDelete = false;
	history.replaceState({}, '', '/w/alpha-lab/conversations');
	vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
		const path = String(input); requests.push({ path, init });
		const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
		if (init?.method === 'POST') {
			if (path.endsWith('/chats')) return response({ ...fixtures.chats['alpha-lab'].items[0], chat_id: 'created-chat',
				conversation_id: 'created-chat', root_conversation_id: 'created-chat', parent_conversation_id: null });
			if (rejectPost) return response({ detail: { code: 'send_failed', message: 'Server rejected this message', details: { retryable: true } } }, 503);
			const chatId = path.split('/chats/')[1].split('/')[0];
			return response({ ...fixtures.chatMessages['chat-1'].items[0], chat_id: chatId,
				conversation_id: chatId, root_conversation_id: chatId, parent_conversation_id: null,
				message_id: 'posted', body: JSON.parse(String(init.body)).body,
				steering: { intent: 'focus', targetsExecutionId: null, percolatedTo: [{ executionId: 'exec-1', intentEpoch: 2 }] } });
		}
		if (init?.method === 'DELETE') return rejectDelete
			? response({ detail: { code: 'delete_failed', message: 'Server could not delete this conversation' } }, 503)
			: new Response(null, { status: 204 });
		if (path.endsWith('/chats')) return response(fixtures.chats['alpha-lab']);
		if (path.includes('/timeline?')) return response({ items: [], nextBefore: null, after: 0 });
		if (path.endsWith('/sub_agents')) return response(path.includes('/chats/chat/') ? backendTree : { chatId: 'chat-1', roots: [], items: [] });
		if (path.endsWith('/execution') || path.includes('/activation/consent/')) return response(null, 404);
		if (path.endsWith('/state')) return response(null, 404);
		return response(null, 404);
	});
});
afterEach(() => { vi.restoreAllMocks(); });

describe.each(['page', 'copilot'])('actual %s host with real API transport', (host) => {
	function mount() {
		return host === 'page'
			? render(ConversationsPage, { props: { data: layoutData('alpha-lab') } })
			: render(CopilotPanel, { props: { workspaceId: 'alpha-lab', onExpand: vi.fn(), onClose: vi.fn() } });
	}
	it('renders two datum cards from one decoded workflow result record through page history and SSE replay', async () => {
		const original = vi.mocked(fetch).getMockImplementation()!;
		const datums = [{ ...aggregateMessage.data.datums[0], datumType: 'workflow_execution', summary: 'Workflow launched',
			structured: { executionId: 'aggregate-workflow', workflowId: 'hypothesis-development', state: 'running', phase: 'discovery' } },
			aggregateMessage.data.datums[1]];
		const workflowResult = { ...aggregateMessage, data: { ...aggregateMessage.data, datums,
			sessionMessage: { ...aggregateMessage.data.sessionMessage, content: { ...aggregateMessage.data.sessionMessage.content,
				result: { datums } } } } };
		const item = (payload: unknown, seq: number) => ({ id: `aggregate-${seq}`, kind: 'message', seq,
			chatId: 'chat-1', workspaceId: 'alpha-lab', ts: aggregateMessage.created_at, threadId: 'root', payload });
		const ownership = { chat_id: 'chat-1', conversation_id: 'chat-1', root_conversation_id: 'chat-1', parent_conversation_id: null };
		const page = decodeWire('TimelinePageWire', { items: [item({ ...aggregateEnvelope, ...ownership, workspace_id: 'alpha-lab' }, 1),
			item({ ...workflowResult, ...ownership, workspace_id: 'alpha-lab' }, 2)], nextBefore: null, after: 2 }, 'aggregate/history');
		vi.mocked(fetch).mockImplementation((input, init) => String(input).includes('/timeline?')
			? Promise.resolve(new Response(JSON.stringify(page))) : original(input, init));
		mount();
		await waitFor(() => expect(screen.getAllByTestId('tool-result-card')).toHaveLength(2));
		await waitFor(() => expect(live.after).toBe(page.after));
		const { TimelineStream } = await vi.importActual<typeof import('$lib/sse')>('$lib/sse');
		const resultItem = page.items[1];
		const bytes = new TextEncoder().encode(`id: 2\nevent: timeline\ndata: ${JSON.stringify(resultItem)}\n\n`);
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: 'alpha-lab', chatId: 'chat-1', after: 0,
			fetchImpl: async () => new Response(new ReadableStream({ start(controller) { controller.enqueue(bytes); } })) });
		await stream.start((decoded) => { expect(decoded).toEqual(resultItem); live.receive!(decoded); stream.stop(); });
		expect(screen.getAllByTestId('tool-result-card')).toHaveLength(2);
		expect(screen.getAllByTestId('chat-message').map((message) => message.dataset.messageId)).toEqual([
			'aggregate-envelope', 'aggregate-result:datum:0', 'aggregate-result:datum:1'
		]);
		expect(screen.getByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.getByTestId('workflow-card-status')).toHaveTextContent('running');
		expect(screen.getAllByTestId('workflow-card')).toHaveLength(1);
		expect(requests.some((request) => request.path.endsWith('/execution'))).toBe(false);
		expect(screen.getByText('Retained compiler diagnostics')).toBeInTheDocument();
		const details = screen.getAllByText('structured');
		await fireEvent.click(details[0]);
		expect(screen.getByText(/missing series/)).toBeInTheDocument();
		expect(screen.getAllByText(/aggregate-study/)).toHaveLength(2);
		expect(page.items.filter((entry) => entry.kind === 'message' && entry.payload.message_id === 'aggregate-result')).toHaveLength(1);
	});
	it('sends through the shared controller and preserves a server failure draft', async () => {
		mount();
		await waitFor(() => expect(live.receive).toBeTypeOf('function'));
		expect(requests.some((request) => request.path.endsWith('/execution'))).toBe(false);
		expect(requests.some((request) => request.path.endsWith('/state'))).toBe(false);
		const input = screen.getByTestId('composer-input');
		await fireEvent.input(input, { target: { value: 'hello from both hosts' } });
		await fireEvent.click(screen.getByTestId('composer-send'));
		await waitFor(() => expect(input).toHaveValue(''));
		const post = requests.find((request) => request.init?.method === 'POST')!;
		expect(post.path).toBe('/api/v1/workspaces/alpha-lab/chats/chat-1/messages');
		expect(JSON.parse(String(post.init!.body)).body).toBe('hello from both hosts');
		rejectPost = true;
		await fireEvent.input(input, { target: { value: 'retain this draft' } });
		await fireEvent.click(screen.getByTestId('composer-send'));
		expect(await screen.findByTestId('composer-error')).toHaveTextContent('Server rejected this message');
		expect(input).toHaveValue('retain this draft');
	});
	it('never shows draft suggestions or a standalone workflow while existing history is pending', async () => {
		const original = vi.mocked(fetch).getMockImplementation()!;
		let releaseList!: () => void;
		let releaseHistory!: () => void;
		vi.mocked(fetch).mockImplementation((input, init) => {
			const path = String(input);
			if (path.endsWith('/chats')) return new Promise((resolve) => {
				releaseList = () => resolve(new Response(JSON.stringify(fixtures.chats['alpha-lab'])));
			});
			if (path.includes('/timeline?')) return new Promise((resolve) => {
				releaseHistory = () => resolve(new Response(JSON.stringify({ items: [{ kind: 'execution', id: 'execution-snapshot', seq: 1,
					chatId: 'chat-1', workspaceId: 'alpha-lab', ts: fixtures.executions['chat-1'].created_at,
					payload: fixtures.executions['chat-1'] }], nextBefore: null, after: 1 })));
			});
			return original(input, init);
		});
		mount();
		await waitFor(() => expect(releaseList).toBeTypeOf('function'));
		expect(screen.queryByTestId('chat-starters')).toBeNull();
		releaseList();
		await waitFor(() => expect(releaseHistory).toBeTypeOf('function'));
		expect(screen.getByTestId('history-loading')).toBeInTheDocument();
		expect(screen.queryByTestId('chat-starters')).toBeNull();
		expect(screen.queryByTestId('workflow-card')).toBeNull();
		expect(live.receive).toBeNull();
		expect(live.after).toBeNull();
		releaseHistory();
		await waitFor(() => expect(screen.queryByTestId('history-loading')).toBeNull());
		await waitFor(() => expect(live.receive).toBeTypeOf('function'));
		expect(live.after).toBe(1);
		expect(await screen.findByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.queryByTestId('chat-starters')).toBeNull();
		await fireEvent.click(screen.getByTestId(host === 'page' ? 'new-conversation' : 'copilot-new-conversation'));
		expect(screen.getByTestId('chat-starters')).toBeInTheDocument();
	});
	it('shows delete failures instead of swallowing them', async () => {
		rejectDelete = true; mount();
		if (host === 'copilot') await fireEvent.click(screen.getByTestId('copilot-history'));
		const deletes = await screen.findAllByTestId(host === 'page' ? 'conversation-delete' : 'copilot-history-delete');
		await fireEvent.click(deletes[0]);
		await fireEvent.click(screen.getByTestId('confirm-delete-conversation-button'));
		expect(await screen.findByTestId('chat-host-error')).toHaveTextContent('Server could not delete');
		expect(screen.queryByTestId('copilot-history-overlay')).toBeNull();
	});
	it('retries the first failed message on the already-persisted conversation', async () => {
		mount();
		await waitFor(() => expect(live.receive).toBeTypeOf('function'));
		await fireEvent.click(screen.getByTestId(host === 'page' ? 'new-conversation' : 'copilot-new-conversation'));
		rejectPost = true;
		const input = screen.getByTestId('composer-input');
		await fireEvent.input(input, { target: { value: 'first message retry' } });
		await fireEvent.click(screen.getByTestId('composer-send'));
		await screen.findByTestId('composer-error');
		rejectPost = false;
		await fireEvent.click(screen.getByTestId('composer-send'));
		await waitFor(() => expect(input).toHaveValue(''));
		expect(requests.filter((request) => request.init?.method === 'POST' && request.path.endsWith('/chats'))).toHaveLength(1);
		expect(requests.filter((request) => request.init?.method === 'POST' && request.path.endsWith('/created-chat/messages'))).toHaveLength(2);
	});
	it('isolates same-ID scopes and drafts on workspace switches and aborts stale history/input without execution fetching', async () => {
		let releaseHistory!: () => void;
		let releasePost!: () => void;
		const oldSignals: AbortSignal[] = [];
		const calls: string[] = [];
		const json = (data: unknown) => new Response(JSON.stringify(data));
		const message = (ws: string, body: string) => ({ ...fixtures.chatMessages['chat-1'].items[0],
			workspace_id: ws, chat_id: 'chat', conversation_id: 'chat', root_conversation_id: 'chat', parent_conversation_id: null,
			message_id: `${ws}-${body}`, body, execution_id: null });
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			const path = String(input); calls.push(path);
			const ws = path.includes('/beta-quant/') ? 'beta-quant' : 'alpha-lab';
			if (path.endsWith('/chats')) return json({ items: [{ ...fixtures.chats['alpha-lab'].items[0], chat_id: 'chat',
				conversation_id: 'chat', root_conversation_id: 'chat', parent_conversation_id: null, workspace_id: ws, title: ws }] });
			if (path.endsWith('/sub_agents')) return json(backendTree);
			if (path.includes('/timeline?')) {
				const scoped = new URL(path, 'http://localhost').searchParams.get('thread') === 'node';
				if (ws === 'alpha-lab' && scoped) {
					oldSignals.push(init!.signal!);
					return new Promise((resolve) => { releaseHistory = () => resolve(json({ items: [{ id: 'stale', chatId: 'chat', workspaceId: ws,
						kind: 'message', seq: 99, ts: '2026-01-01T00:00:00Z', threadId: 'node', payload: { ...message(ws, 'STALE HISTORY'),
							conversation_id: 'node', parent_conversation_id: 'chat', data: { subAgentId: 'node' } } }], nextBefore: null, after: 99 })); });
				}
				const payload = { ...message(ws, 'node handle'), role: 'tool', tool_result: { tool: 'spawn_sub_agent', callId: 'spawn',
					ok: true, summary: 'Agent', structured: { subAgentId: 'node', role: 'researcher' }, text: '', failure: null, datumType: 'sub_agent_handle' } };
				return json({ items: scoped ? [] : [{ id: `${ws}-handle`, chatId: 'chat', workspaceId: ws, kind: 'message', seq: 1,
					ts: payload.created_at, threadId: 'root', payload }], nextBefore: null, after: 1 });
			}
			if (path.endsWith('/node/messages')) {
				const receipt = { ...message(ws, 'accepted'), kind: 'sub_agent', subAgentId: 'node', data: { subAgentId: 'node' },
					conversation_id: 'node', parent_conversation_id: 'chat',
					operatorPolicy: { allowed: true, actorId: 'operator', actorKind: 'human_operator', allowedActorKinds: ['human_operator'],
						policy: 'human_only', role: 'researcher', nodeId: 'node' } };
				if (ws === 'alpha-lab') {
					oldSignals.push(init!.signal!);
					return new Promise((resolve) => { releasePost = () => resolve(json(receipt)); });
				}
				return json(receipt);
			}
			return new Response(null, { status: 404 });
		});
		const view = mount();
		await fireEvent.input(await screen.findByTestId('composer-input'), { target: { value: 'private root A' } });
		await fireEvent.click(await screen.findByTestId('sub-agent-card'));
		const oldInput = await screen.findByTestId('sub-agent-composer-input');
		await fireEvent.input(oldInput, { target: { value: 'private node A' } });
		await waitFor(() => expect(releaseHistory).toBeTypeOf('function'));
		await fireEvent.click(screen.getByTestId('sub-agent-composer-send'));
		await waitFor(() => expect(releasePost).toBeTypeOf('function'));
		if (host === 'page') history.replaceState({}, '', '/w/beta-quant/conversations');
		await view.rerender(host === 'page' ? { data: layoutData('beta-quant') }
			: { workspaceId: 'beta-quant', onExpand: vi.fn(), onClose: vi.fn() });
		expect(await screen.findByTestId('composer-input')).toHaveValue('');
		await waitFor(() => expect(calls).toContain('/api/v1/workspaces/beta-quant/chats/chat/sub_agents'));
		expect(oldSignals).toHaveLength(2);
		expect(calls.some((path) => path.endsWith('/execution'))).toBe(false);
		expect(oldSignals.every((signal) => signal.aborted)).toBe(true);
		releaseHistory(); releasePost();
		await fireEvent.click(await screen.findByTestId('sub-agent-card'));
		const input = await screen.findByTestId('sub-agent-composer-input');
		expect(input).toHaveValue('');
		await fireEvent.input(input, { target: { value: 'feedback B' } });
		await fireEvent.click(screen.getByTestId('sub-agent-composer-send'));
		await waitFor(() => expect(input).toHaveValue(''));
		expect(calls).toContain('/api/v1/workspaces/beta-quant/chats/chat/sub_agents/node/messages');
		expect(screen.queryByText('STALE HISTORY')).toBeNull();
		expect(screen.queryByTestId('node-send-error')).toBeNull();
	});
	it('aborts pending old-workspace list/tree reads and drops their late responses', async () => {
		const original = vi.mocked(fetch).getMockImplementation()!;
		let releaseList!: () => void;
		let listSignal!: AbortSignal;
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			if (String(input) === '/api/v1/workspaces/alpha-lab/chats') {
				listSignal = init!.signal!;
				return new Promise((resolve) => { releaseList = () => resolve(new Response(JSON.stringify({ items: [
					{ ...fixtures.chats['alpha-lab'].items[0], title: 'STALE LIST' }
				] }))); });
			}
			return original(input, init);
		});
		const view = mount();
		await waitFor(() => expect(releaseList).toBeTypeOf('function'));
		if (host === 'page') history.replaceState({}, '', '/w/beta-quant/conversations');
		await view.rerender(host === 'page' ? { data: layoutData('beta-quant') }
			: { workspaceId: 'beta-quant', onExpand: vi.fn(), onClose: vi.fn() });
		await waitFor(() => expect(listSignal.aborted).toBe(true));
		releaseList();
		await waitFor(() => expect(requests.some((request) => request.path.includes('/beta-quant/') && request.path.endsWith('/sub_agents'))).toBe(true));
		expect(screen.queryByText('STALE LIST')).toBeNull();

		let releaseTree!: () => void;
		let treeSignal!: AbortSignal;
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			if (String(input).includes('/alpha-lab/') && String(input).endsWith('/sub_agents')) {
				treeSignal = init!.signal!;
				return new Promise((resolve) => { releaseTree = () => resolve(new Response(JSON.stringify(backendTree))); });
			}
			return original(input, init);
		});
		if (host === 'page') history.replaceState({}, '', '/w/alpha-lab/conversations');
		await view.rerender(host === 'page' ? { data: layoutData('alpha-lab') }
			: { workspaceId: 'alpha-lab', onExpand: vi.fn(), onClose: vi.fn() });
		await waitFor(() => expect(releaseTree).toBeTypeOf('function'));
		if (host === 'page') history.replaceState({}, '', '/w/beta-quant/conversations');
		await view.rerender(host === 'page' ? { data: layoutData('beta-quant') }
			: { workspaceId: 'beta-quant', onExpand: vi.fn(), onClose: vi.fn() });
		await waitFor(() => expect(treeSignal.aborted).toBe(true));
		releaseTree();
		expect(await screen.findByTestId('composer-input')).toHaveValue('');
		expect(screen.queryByTestId('sub-agent-composer-input')).toBeNull();
	});
});

it('records normalized percolation once in the shared controller', async () => {
	const host = new ChatHostController();
	await host.open('alpha-lab');
	await host.send({ body: 'focus', steeringIntent: 'focus' });
	try {
		expect(host.percolations.posted).toEqual([{ execution_id: 'exec-1', intent_epoch: 2, steering_intent: 'focus' }]);
	} finally { host.stop(); }
});

it('opens the midturn composer from a genuine decoded backend reentry tree despite prior completion', async () => {
	const timeline = new TimelineController();
	timeline.start('alpha-lab', 'chat');
	timeline.merge([{
		id: 'old-completed', chatId: 'chat', workspaceId: 'alpha-lab', kind: 'event', seq: 3,
		ts: '2026-01-01T00:00:03Z', threadId: 'node', executionId: 'exec',
		payload: { eventId: 'old-completed', eventType: 'sub_agent.completed', cursor: 3,
			executionId: 'exec', workspaceId: 'alpha-lab', occurredAt: '2026-01-01T00:00:03Z',
			actorId: 'agent', actorScope: 'assistant', correlationId: 'old', intentEpoch: 0, lateResult: false,
			payload: { subAgentId: 'node', phaseAttemptId: 'attempt-a' } }
	}]);
	render(ChatSurface, { props: { workspaceId: 'alpha-lab', chatId: 'chat', timeline, onSend: vi.fn(),
		initialScope: 'conversation:chat/sub_agent:node' } });
	try {
		const input = await screen.findByTestId('sub-agent-composer-input');
		await fireEvent.input(input, { target: { value: 'feedback while reopened' } });
		expect(input).not.toBeDisabled();
		expect(screen.getByTestId('sub-agent-composer-send')).not.toBeDisabled();
		expect(screen.queryByTestId('node-metadata-error')).toBeNull();
	} finally { timeline.stop(); }
});
