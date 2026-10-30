import { transcriptItems, nodeSummaries } from './read-fixtures';
import { expect, test, type Page } from '@playwright/test';
import chats from '../src/lib/fixtures/chats.json' with { type: 'json' };
import seed from '../src/lib/fixtures/chat-messages.json' with { type: 'json' };
import executions from '../src/lib/fixtures/executions.json' with { type: 'json' };
import workspaces from '../src/lib/fixtures/workspaces.json' with { type: 'json' };
import snapshots from '../src/lib/fixtures/workspace-snapshot.json' with { type: 'json' };
import backendTree from '../src/tests/backend-tree.json' with { type: 'json' };
import type { TimelineItemWire, ConversationOutlineEntryWire } from '../src/lib/generated';

const chat = chats['alpha-lab'].items[0];
const workspace = workspaces.items.find((item) => item.id === 'alpha-lab')!;
const rootUrl = `/w/${workspace.id}/conversations?chat=${chat.chat_id}`;
const time = '2026-10-28T12:00:00Z';

function message(seq: number, id: string, thread: string, body: string, role = 'assistant'): TimelineItemWire {
	return { id, seq, kind: 'message', chatId: chat.chat_id, workspaceId: workspace.id, threadId: thread, ts: time,
		payload: { ...seed['chat-1'].items[0], message_id: id, conversation_id: thread === 'root' ? chat.chat_id : thread,
			parent_conversation_id: thread === 'root' ? null : 'discovery', body, role, execution_id: null,
			kind: thread === 'root' ? 'message' : 'sub_agent', data: thread === 'root' ? null : { subAgentId: thread } } } as TimelineItemWire;
}

async function scenario(page: Page, scoped: boolean) {
	const historyRequests: URL[] = [];
	const frames: TimelineItemWire[] = [];
	const node = (id: string, parent: string | null, role: string, execution: string, phase: string | null, acceptsInput: boolean) => ({
		...backendTree.items[0], subAgentId: id, chatId: chat.chat_id, parentSubAgentId: parent,
		returnsToSubAgentId: parent, role, workflowExecutionId: execution, phase, phaseExecutor: !!phase,
		phaseAttemptId: phase ? `${execution}:${phase}:1` : null, acceptsInput, children: []
	});
	const nodes = [node('main-record', null, 'workflow-node', 'main', null, false),
		node('discovery', 'main-record', 'Thesis discovery orchestrator', 'main', 'discovery', true),
		node('branch-record', 'discovery', 'workflow-node', 'branch', null, false),
		node('critique', 'branch-record', 'Research focused critique', 'branch', 'focused_critique', false)];
	const rootHistory = [message(1, 'turn-1', 'root', 'Explore MSFT opportunities', 'user'),
		...Array.from({ length: 32 }, (_, index) => message(index + 10, `root-${index}`, 'root', `Root response ${index}\n\n${'Retained conversation context. '.repeat(16)}`)),
		message(50, 'turn-2', 'root', 'A later operator request', 'user')];
	const critiqueHistory = Array.from({ length: 32 }, (_, index) => message(100 + index, `critique-${index}`, 'critique', `Critique response ${index}\n\n${'Retained evidence and interpretation. '.repeat(16)}`));
	const artifact = message(80, 'old-artifact', 'critique', 'Read metrics', 'tool');
	if (artifact.kind === 'message') artifact.payload.data = { activityKind: 'delivery', subAgentId: 'critique', sessionMessage: {
		message_id: 'old-artifact', direction: 'incoming', position: 1,
		content: { source: 'tool', text: 'Read metrics', requests: [], correlation: null,
			result: { ok: true, operation: 'read', datums: [{ datumType: 'read_artifact', ok: true,
				structured: { job_id: 'job-1', name: 'metrics.json', content: 'Retained artifact content' },
				text: 'Retained artifact content', summary: 'Read metrics', failure: null }] } }
	} };
	const outlineBase = { datumIndex: null, entityId: null, executionId: null, requestMessageId: null, status: null };
	const outline: ConversationOutlineEntryWire[] = [
		{ ...outlineBase, id: 'turn-1', kind: 'turn', title: 'Explore MSFT opportunities', seq: 1, threadId: 'root', messageId: 'turn-1' },
		{ ...outlineBase, id: 'turn-2', kind: 'turn', title: 'A later operator request', seq: 50, threadId: 'root', messageId: 'turn-2' },
		{ ...outlineBase, id: 'old-artifact:datum:0', kind: 'artifact', title: 'metrics.json', seq: 80, threadId: 'critique', messageId: 'old-artifact', datumIndex: 0, entityId: 'job-1:metrics.json' }
	];
	const metadata = [{ ...executions['chat-1'], execution_id: 'main', trigger_message_id: 'turn-1', workflow_id: 'hypothesis-development', workflow_version: '1', active_phases: ['discovery'] },
		{ ...executions['chat-1'], execution_id: 'branch', conversation_id: 'discovery', parent_conversation_id: chat.chat_id, trigger_message_id: 'unknown-child-seed', workflow_id: 'research-investigation', workflow_version: '1', active_phases: ['focused_critique'] }];
	// Both hosts use the production timeline and real DOM layout; only external HTTP is scripted.
	await page.addInitScript(() => localStorage.setItem('thesis.backend', 'local'));
	await page.route('**/api/**', async (route) => {
		const url = new URL(route.request().url());
		const path = url.pathname;
		if (path.endsWith('/timeline/stream')) return route.fulfill({ contentType: 'text/event-stream', body: frames.filter((item) => item.seq > Number(url.searchParams.get('after') ?? 0)).map((item) => `id: ${item.seq}\nevent: timeline\ndata: ${JSON.stringify(item)}\n\n`).join('') || ': idle\n\n' });
		if (path.endsWith('/timeline')) {
			historyRequests.push(url);
			const thread = url.searchParams.get('thread');
			return route.fulfill({ json: { items: transcriptItems(thread === 'critique' ? url.searchParams.has('before') ? [artifact] : critiqueHistory : thread === 'discovery' ? [message(60, 'parent-history', 'discovery', 'Exact parent conversation')] : rootHistory), nextBefore: null, after: 150 } });
		}
		if (path.endsWith('/outline')) return route.fulfill({ json: { items: outline.filter((item) => item.seq > Number(url.searchParams.get('after') ?? 0)), after: 150, nextAfter: null } });
		if (path.endsWith('/executions')) return route.fulfill({ json: { items: metadata, after: 150 } });
		if (path.endsWith('/sub_agents')) return route.fulfill({ json: { chatId: chat.chat_id, items: nodeSummaries(nodes) } });
		if (path.includes('/workflows/')) {
			const id = path.split('/').at(-1);
			const phase = id === 'hypothesis-development' ? 'discovery' : 'focused_critique';
			return route.fulfill({ json: { workflow_id: id, version: '1', title: id, phases: [phase, 'completed'], terminal_phases: ['completed'], inputs: {}, routes: { [phase]: { completed: ['completed.input'] }, completed: {} } } });
		}
		if (path.endsWith('/chats')) return route.fulfill({ json: { items: [chat] } });
		if (path.endsWith(`/chats/${chat.chat_id}`)) return route.fulfill({ json: chat });
		if (path.endsWith('/workspaces')) return route.fulfill({ json: { items: [workspace] } });
		if (path.endsWith(`/workspaces/${workspace.id}`)) return route.fulfill({ json: workspace });
		if (path.endsWith('/snapshot')) return route.fulfill({ json: snapshots['alpha-lab'] });
		return route.fulfill({ status: 404, json: { detail: 'Not part of this scenario' } });
	});
	await page.goto(scoped ? `${rootUrl}&scope=${encodeURIComponent(`conversation:${chat.chat_id}/sub_agent:critique`)}` : rootUrl);
	await expect(page.getByTestId(scoped ? 'scope-transcript-scroll' : 'transcript-scroll')).toBeVisible();
	return { frames, historyRequests };
}

test('compact outline reveals unloaded nested artifacts and each breadcrumb opens the exact parent', async ({ page }, testInfo) => {
	test.setTimeout(30_000);
	await page.setViewportSize({ width: 1440, height: 850 });
	const { historyRequests } = await scenario(page, false);
	const rail = page.getByTestId('conversation-outline');
	const artifact = rail.getByRole('button', { name: 'metrics.json, artifact' });
	await expect(artifact).toBeVisible();
	const handle = page.getByRole('separator', { name: 'Resize conversation outline' });
	const initialWidth = (await rail.boundingBox())!.width;
	await handle.focus();
	await page.keyboard.press('ArrowLeft');
	await expect.poll(async () => (await rail.boundingBox())!.width).toBe(initialWidth + 20);
	const divider = (await handle.boundingBox())!;
	await page.mouse.move(divider.x + divider.width / 2, divider.y + divider.height / 2);
	await page.mouse.down();
	await page.mouse.move(divider.x - 30, divider.y + divider.height / 2);
	await page.mouse.up();
	expect((await rail.boundingBox())!.width).toBeGreaterThan(initialWidth + 40);
	const preferredWidth = (await rail.boundingBox())!.width;
	await page.getByTestId('outline-toggle').click();
	await expect(rail).toHaveCount(0);
	await page.getByTestId('outline-toggle').click();
	await expect.poll(async () => (await rail.boundingBox())!.width).toBe(preferredWidth);
	await rail.getByLabel('Toggle Hypothesis development', { exact: true }).click({ position: { x: 8, y: 12 } });
	await expect(artifact).not.toBeVisible();
	await rail.getByLabel('Toggle Hypothesis development', { exact: true }).click({ position: { x: 8, y: 12 } });
	await expect(artifact).toBeVisible();
	await rail.getByLabel('Toggle Research focused critique', { exact: true }).click({ position: { x: 48, y: 12 } });
	await expect(artifact).not.toBeVisible();
	await rail.getByLabel('Toggle Research focused critique', { exact: true }).click({ position: { x: 48, y: 12 } });
	await expect(artifact).toBeVisible();
	const row = await artifact.boundingBox();
	expect(row!.height).toBe(24);
	expect(historyRequests.every((url) => url.searchParams.get('thread') === 'root')).toBe(true);
	await rail.getByRole('button', { name: 'Hypothesis development, workflow' }).click();
	await expect(page.locator('[data-testid="workflow-card"][data-execution="main"]')).toBeInViewport();
	await expect(artifact).toBeVisible();
	await page.getByTestId('transcript-scroll').evaluate((element) => { element.scrollTop = 100; });
	await expect(page.getByTestId('jump-to-bottom')).toBeVisible();
	// Late nested evidence belongs to the launch request, even after turn two.
	await expect(rail.locator('details.outline-turn').filter({ has: page.getByRole('button', { name: 'metrics.json, artifact' }) }).locator('summary').first()).toHaveText('Explore MSFT opportunities');
	await rail.getByRole('button', { name: 'Thesis discovery orchestrator, conversation' }).click();
	await expect(page.getByText('Exact parent conversation', { exact: true })).toBeVisible();
	await expect(artifact).toBeVisible();
	await artifact.click();
	await expect(page.getByText('Retained artifact content', { exact: true }).first()).toBeVisible();
	await expect(page.getByTestId('scope-crumb').last()).toHaveText('Research focused critique');
	await expect(page.getByTestId('scope-crumb')).toHaveCount(3);
	await page.screenshot({ path: testInfo.outputPath('conversation-outline.png') });
	expect(historyRequests.some((url) => url.searchParams.get('before') === '81' && url.searchParams.get('thread') === 'critique')).toBe(true);
	await page.getByTestId('scope-crumb').filter({ hasText: 'Thesis discovery orchestrator' }).click();
	await expect(page.getByText('Exact parent conversation', { exact: true })).toBeVisible();
	await expect(page.getByTestId('sub-agent-composer-input')).toBeVisible();
	await page.getByTestId('scope-back').click();
	await expect(page.getByTestId('scope-crumb').last()).toHaveText('Conversation');
	await expect(page.getByTestId('composer-input')).toBeVisible();
	await expect.poll(() => page.getByTestId('transcript-scroll').evaluate((element) => element.scrollTop)).toBe(100);
	await page.setViewportSize({ width: 390, height: 844 });
	expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
	await expect(rail).toBeInViewport();
	expect((await rail.boundingBox())!.y + (await rail.boundingBox())!.height).toBeLessThanOrEqual(844);
	await rail.locator('.outline-scroll').evaluate((element) => { element.scrollTop = element.scrollHeight; });
	await expect(artifact).toBeInViewport();
	await page.screenshot({ path: testInfo.outputPath('conversation-outline-mobile.png') });
});

test('seamless shell keeps the rail minimal and conversation selection independent of drafts', async ({ page }, testInfo) => {
	test.setTimeout(30_000);
	await page.setViewportSize({ width: 1440, height: 850 });
	await scenario(page, false);
	const sidebar = page.getByTestId('sidebar');
	const picker = page.getByTestId('conversation-picker');
	const toggle = page.getByTestId('conversation-picker-toggle');
	const composer = page.getByTestId('composer-input');
	const transcript = page.getByTestId('transcript-scroll');
	await expect.poll(async () => (await sidebar.boundingBox())!.width).toBe(56);
	await expect(page.getByTestId('nav-conversations')).toHaveAttribute('aria-current', 'page');
	expect(await page.locator('#main-content').evaluate((element) => getComputedStyle(element).padding)).toBe('0px');
	const readingWidth = (await transcript.boundingBox())!.width;
	await composer.fill('Keep this draft while rearranging the workspace.');
	await toggle.click();
	await expect(picker).toBeHidden();
	await expect.poll(async () => (await transcript.boundingBox())!.width).toBe(readingWidth + 192);
	await expect(composer).toHaveValue('Keep this draft while rearranging the workspace.');
	await toggle.click();
	await expect(picker).toBeVisible();
	await expect(composer).toHaveValue('Keep this draft while rearranging the workspace.');
	await page.getByRole('button', { name: 'Expand navigation', exact: true }).click();
	await expect.poll(async () => (await sidebar.boundingBox())!.width).toBe(240);
	await page.reload();
	await expect.poll(async () => (await sidebar.boundingBox())!.width).toBe(240);
	await page.getByRole('button', { name: 'Collapse navigation', exact: true }).click();
	await page.reload();
	await expect.poll(async () => (await sidebar.boundingBox())!.width).toBe(56);
	await page.getByTestId('workspace-switcher-button').click();
	await expect(page.getByTestId('workspace-menu')).toBeVisible();
	await expect(page.getByTestId('create-workspace-trigger')).toBeVisible();
	await expect(page.getByTestId('delete-workspace-trigger')).toBeVisible();
	await page.keyboard.press('Escape');
	await expect(page.getByTestId('workspace-menu')).toHaveCount(0);
	await page.getByTestId('search-trigger').click();
	await expect(page.getByTestId('command-palette')).toBeVisible();
	await page.keyboard.press('Escape');
	await page.getByTestId('settings-trigger').click();
	await expect(page.getByRole('dialog', { name: 'Backend settings', exact: true })).toBeVisible();
	await page.keyboard.press('Escape');
	await page.screenshot({ path: testInfo.outputPath('seamless-workspace.png') });
	await page.setViewportSize({ width: 390, height: 844 });
	await expect(picker).toBeHidden();
	await toggle.click();
	await expect(picker).toBeVisible();
	await page.getByTestId('new-conversation').click();
	await expect(picker).toBeHidden();
	await expect(page.getByRole('heading', { name: 'New conversation', exact: true })).toBeVisible();
	await composer.fill('A mobile draft');
	await toggle.click();
	await page.keyboard.press('Escape');
	await expect(picker).toBeHidden();
	await expect(composer).toHaveValue('A mobile draft');
	expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
	await expect(composer).toBeInViewport();
	await page.screenshot({ path: testInfo.outputPath('seamless-workspace-mobile.png') });
	await page.setViewportSize({ width: 320, height: 844 });
	expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
	await page.getByTestId('workspace-switcher-button').click();
	await expect(page.getByTestId('workspace-menu')).toBeInViewport();
	const menu = (await page.getByTestId('workspace-menu').boundingBox())!;
	expect(menu.x + menu.width).toBeLessThanOrEqual(320);
});

for (const scoped of [false, true]) {
	test(`arrow hides at bottom, follows growth, and stops when reading earlier (${scoped ? 'child' : 'root'})`, async ({ page }) => {
		test.setTimeout(30_000);
		await page.setViewportSize({ width: 1440, height: 850 });
		const { frames } = await scenario(page, scoped);
		const viewport = page.getByTestId(scoped ? 'scope-transcript-scroll' : 'transcript-scroll');
		const bottomDistance = () => viewport.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
		await expect.poll(bottomDistance).toBeLessThanOrEqual(4);
		await expect(page.getByTestId('jump-to-bottom')).toHaveCount(0);
		frames.push(message(151, 'live-first', scoped ? 'critique' : 'root', 'New agent response. '.repeat(50)));
		await expect(page.locator('[data-message-id="live-first"]')).toBeAttached();
		await expect.poll(bottomDistance).toBeLessThanOrEqual(4);
		await viewport.evaluate((element) => { element.scrollTop = 100; });
		const arrow = page.getByRole('button', { name: 'Jump to bottom', exact: true });
		await expect(arrow).toBeVisible();
		await expect(arrow).toHaveText('');
		frames.push(message(152, 'live-second', scoped ? 'critique' : 'root', 'Another response. '.repeat(50)));
		await expect(page.locator('[data-message-id="live-second"]')).toBeAttached();
		expect(await viewport.evaluate((element) => element.scrollTop)).toBe(100);
		await arrow.click();
		await expect(arrow).toHaveCount(0);
		await expect.poll(bottomDistance).toBeLessThanOrEqual(4);
		frames.push(message(153, 'live-third', scoped ? 'critique' : 'root', 'Latest response. '.repeat(50)));
		await expect(page.locator('[data-message-id="live-third"]')).toBeAttached();
		await expect.poll(bottomDistance).toBeLessThanOrEqual(4);
	});
}
