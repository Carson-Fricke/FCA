import { screen } from '@testing-library/svelte';
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { WorkflowDefinition, WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$app/state', () => ({ page: { params: { workspace: 'route-workspace' } } }));

const definition: WorkflowDefinition = {
	workflow_id: 'performance-workflow', version: '1.0.0', title: 'Shared topology',
	splits: {},
	phases: ['review', 'completed'], terminal_phases: ['completed'],
	transitions: { review: ['completed'], completed: [] }
};
const execution: WorkflowExecutionRecord = {
	execution_id: 'exec-1', chat_id: 'chat-1', trigger_message_id: 'msg-1',
	workflow_id: definition.workflow_id, workflow_version: definition.version,
	workspace_id: 'workspace-a', requested_by: 'operator', state: 'running',
	control_sequence: 0, active_phases: ['review'], intent_epoch: 0, steering_message_ids: [], created_at: ''
};
const phases: WorkflowPhase[] = [{ name: 'review', status: 'active' }];
const options = { workspaceId: 'workspace-a', version: definition.version };

let api: typeof import('$lib/api');
let Card: typeof import('$lib/components/chat/WorkflowCard.svelte').default;
let fetchMock: ReturnType<typeof vi.fn>;
let render: typeof import('@testing-library/svelte').render;
let cleanup: typeof import('@testing-library/svelte').cleanup;
let flushSync: typeof import('svelte').flushSync;
let tick: typeof import('svelte').tick;

beforeEach(async () => {
	localStorage.clear();
	vi.stubEnv('PUBLIC_THESIS_API_BASE', '/api');
	vi.resetModules();
	({ render, cleanup } = await import('@testing-library/svelte'));
	({ flushSync, tick } = await import('svelte'));
	api = await import('$lib/api');
	Card = (await import('$lib/components/chat/WorkflowCard.svelte')).default;
	fetchMock = vi.fn(async () => new Response(JSON.stringify(definition)));
	vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
	vi.unstubAllEnvs();
});
afterAll(() => vi.unstubAllGlobals());

describe('shared workflow definition reads', () => {
	it('makes one HTTP call for two cards, and reuses the successful version on remount', async () => {
		let resolve!: (response: Response) => void;
		fetchMock.mockImplementationOnce(() => new Promise<Response>((done) => { resolve = done; }));
		const started = performance.now();
		const first = render(Card, { props: { execution, phases } });
		render(Card, { props: { execution: { ...execution, execution_id: 'exec-2' }, phases } });
		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/workflows/performance-workflow?version=1.0.0');
		resolve(new Response(JSON.stringify(definition)));
		await screen.findAllByText(definition.title);
		const coldMs = performance.now() - started;
		first.unmount();
		const warmStarted = performance.now();
		render(Card, { props: { execution, phases } });
		await tick();
		expect(screen.getAllByText(definition.title)).toHaveLength(2);
		expect(fetchMock).toHaveBeenCalledTimes(1);
		console.info(`WorkflowCard mock workload: two cold mounts ${coldMs.toFixed(2)}ms; cached remount ${(performance.now() - warmStarted).toFixed(2)}ms; HTTP calls 1.`);
	});

	it.each([404, 409, 500])('does not cache HTTP %i failures and shares the retry', async (status) => {
		fetchMock.mockResolvedValueOnce(new Response('{}', { status }));
		const reads = [api.getWorkflowDefinition(definition.workflow_id, options),
			api.getWorkflowDefinition(definition.workflow_id, options)];
		const failed = await Promise.allSettled(reads);
		expect(failed.every((result) => result.status === 'rejected')).toBe(true);
		expect(fetchMock).toHaveBeenCalledTimes(1);
		await Promise.all([api.getWorkflowDefinition(definition.workflow_id, options),
			api.getWorkflowDefinition(definition.workflow_id, options)]);
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('does not cache transport or wire-decode errors', async () => {
		fetchMock.mockRejectedValueOnce(new Error('offline'))
			.mockResolvedValueOnce(new Response('{}'));
		await expect(api.getWorkflowDefinition(definition.workflow_id, options)).rejects.toThrow('offline');
		await expect(api.getWorkflowDefinition(definition.workflow_id, options)).rejects.toMatchObject({ name: 'WireDecodeError' });
		await expect(api.getWorkflowDefinition(definition.workflow_id, options)).resolves.toEqual(definition);
		expect(fetchMock).toHaveBeenCalledTimes(3);
	});

	it('does not pin current-version reads or mismatched version responses', async () => {
		await Promise.all([api.getWorkflowDefinition(definition.workflow_id), api.getWorkflowDefinition(definition.workflow_id)]);
		await api.getWorkflowDefinition(definition.workflow_id);
		expect(fetchMock).toHaveBeenCalledTimes(2);
		const oldVersion = { ...options, version: '0.9.0' };
		await api.getWorkflowDefinition(definition.workflow_id, oldVersion);
		await api.getWorkflowDefinition(definition.workflow_id, oldVersion);
		expect(fetchMock).toHaveBeenCalledTimes(4);
		expect(fetchMock.mock.calls[2][0]).toContain('?version=0.9.0');
	});

	it('isolates workspace and requested version keys without changing the global route', async () => {
		await api.getWorkflowDefinition(definition.workflow_id, options);
		await api.getWorkflowDefinition(definition.workflow_id, { ...options, workspaceId: 'workspace-b' });
		await api.getWorkflowDefinition(definition.workflow_id, { ...options, version: '2.0.0' });
		await api.getWorkflowDefinition(definition.workflow_id, options);
		expect(fetchMock).toHaveBeenCalledTimes(3);
		expect(fetchMock.mock.calls[1][0]).toBe(fetchMock.mock.calls[0][0]);
	});

	it('expires versioned definitions after five minutes', async () => {
		const now = vi.spyOn(Date, 'now').mockReturnValue(1_000);
		await api.getWorkflowDefinition(definition.workflow_id, options);
		now.mockReturnValue(300_999);
		await api.getWorkflowDefinition(definition.workflow_id, options);
		expect(fetchMock).toHaveBeenCalledTimes(1);
		now.mockReturnValue(301_000);
		await api.getWorkflowDefinition(definition.workflow_id, options);
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('bounds retained definitions to 64 with least-recently-used eviction', async () => {
		fetchMock.mockImplementation(async (url: string) => new Response(JSON.stringify({
			...definition, workflow_id: new URL(url, 'http://localhost').pathname.split('/').at(-1)
		})));
		for (let index = 0; index < 64; index++) await api.getWorkflowDefinition(`workflow-${index}`, options);
		await api.getWorkflowDefinition('workflow-0', options);
		await api.getWorkflowDefinition('workflow-64', options);
		await api.getWorkflowDefinition('workflow-0', options);
		expect(fetchMock).toHaveBeenCalledTimes(65);
		await api.getWorkflowDefinition('workflow-1', options);
		expect(fetchMock).toHaveBeenCalledTimes(66);
	});

	it('bounds tracked pending reads and does not reinsert an evicted late result', async () => {
		let resolve!: (response: Response) => void;
		fetchMock.mockImplementationOnce(() => new Promise<Response>((done) => { resolve = done; }));
		const pending = api.getWorkflowDefinition(definition.workflow_id, options);
		for (let index = 0; index < 64; index++) {
			await api.getWorkflowDefinition(definition.workflow_id, { ...options, workspaceId: `workspace-${index}` });
		}
		resolve(new Response(JSON.stringify(definition)));
		await pending;
		await api.getWorkflowDefinition(definition.workflow_id, options);
		expect(fetchMock).toHaveBeenCalledTimes(66);
	});

	it('does not carry cached state across backend kind or API base reloads', async () => {
		await api.getWorkflowDefinition(definition.workflow_id, options);
		localStorage.setItem('thesis.backend', 'azure');
		localStorage.setItem('thesis.azure_base', 'https://backend-a.example');
		vi.resetModules();
		const remote = await import('$lib/api');
		await remote.getWorkflowDefinition(definition.workflow_id, options);
		expect(fetchMock.mock.calls[1][0]).toBe('https://backend-a.example/v1/workflows/performance-workflow?version=1.0.0');
		localStorage.setItem('thesis.azure_base', 'https://backend-b.example');
		vi.resetModules();
		await (await import('$lib/api')).getWorkflowDefinition(definition.workflow_id, options);
		expect(fetchMock.mock.calls[2][0]).toBe('https://backend-b.example/v1/workflows/performance-workflow?version=1.0.0');
	});
});

describe('workflow canvas scheduling', () => {
	it('schedules no frames offscreen/hidden, resumes once visible, and cleans up on unmount', async () => {
		let intersect!: (entries: { isIntersecting: boolean }[]) => void;
		const disconnect = vi.fn();
		vi.stubGlobal('IntersectionObserver', class {
			constructor(callback: typeof intersect) { intersect = callback; }
			observe = vi.fn();
			disconnect = disconnect;
		});
		let nextId = -1;
		const frames = new Map<number, FrameRequestCallback>();
		const schedule = vi.fn((callback: FrameRequestCallback) => {
			frames.set(++nextId, callback);
			return nextId;
		});
		vi.stubGlobal('requestAnimationFrame', schedule);
		vi.stubGlobal('cancelAnimationFrame', vi.fn((id: number) => { frames.delete(id); }));
		let hidden = false;
		vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
		const { unmount, rerender } = render(Card, { props: { execution, phases } });
		await screen.findByText(definition.title);
		expect(schedule).not.toHaveBeenCalled();
		flushSync(() => intersect([{ isIntersecting: true }]));
		expect(frames.size).toBe(1);
		const [id, frame] = [...frames][0];
		frames.delete(id);
		frame(16);
		expect(frames.size).toBe(1);
		flushSync(() => intersect([{ isIntersecting: false }]));
		expect(frames.size).toBe(0);
		const offscreenCount = schedule.mock.calls.length;
		await rerender({ execution, phases: [{ name: 'review', status: 'active', attempts: 2 }] });
		expect(schedule).toHaveBeenCalledTimes(offscreenCount);
		flushSync(() => {
			hidden = true;
			document.dispatchEvent(new Event('visibilitychange'));
			intersect([{ isIntersecting: true }]);
		});
		expect(frames.size).toBe(0);
		flushSync(() => {
			hidden = false;
			document.dispatchEvent(new Event('visibilitychange'));
		});
		expect(frames.size).toBe(1);
		flushSync(() => {
			hidden = true;
			document.dispatchEvent(new Event('visibilitychange'));
		});
		expect(frames.size).toBe(0);
		flushSync(() => {
			hidden = false;
			document.dispatchEvent(new Event('visibilitychange'));
		});
		expect(frames.size).toBe(1);
		unmount();
		expect(frames.size).toBe(0);
		expect(disconnect).toHaveBeenCalledOnce();
		const finalCount = schedule.mock.calls.length;
		document.dispatchEvent(new Event('visibilitychange'));
		await tick();
		expect(schedule).toHaveBeenCalledTimes(finalCount);
	});

	it('keeps static graphs idle and supports visibility changes without IntersectionObserver', async () => {
		vi.stubGlobal('IntersectionObserver', undefined);
		const schedule = vi.fn(() => 0);
		const cancel = vi.fn();
		vi.stubGlobal('requestAnimationFrame', schedule);
		vi.stubGlobal('cancelAnimationFrame', cancel);
		let hidden = false;
		vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
		const { rerender } = render(Card, { props: { execution,
			phases: [{ name: 'review', status: 'done' }] } });
		await screen.findByText(definition.title);
		expect(schedule).not.toHaveBeenCalled();
		await rerender({ execution, phases: [{ name: 'review', status: 'done' }], attentionPhases: ['review'] });
		const activeCount = schedule.mock.calls.length;
		expect(activeCount).toBeGreaterThan(0);
		flushSync(() => {
			hidden = true;
			document.dispatchEvent(new Event('visibilitychange'));
		});
		expect(cancel).toHaveBeenCalledWith(0);
		flushSync(() => {
			hidden = false;
			document.dispatchEvent(new Event('visibilitychange'));
		});
		expect(schedule).toHaveBeenCalledTimes(activeCount + 1);
		await rerender({ execution, phases: [{ name: 'review', status: 'done' }], attentionPhases: [] });
		expect(schedule).toHaveBeenCalledTimes(activeCount + 1);
	});
});
