import { transcriptItems, nodeSummaries } from './read-fixtures';
import { expect, test } from '@playwright/test';
import chats from '../src/lib/fixtures/chats.json' with { type: 'json' };
import messages from '../src/lib/fixtures/chat-messages.json' with { type: 'json' };
import workspaces from '../src/lib/fixtures/workspaces.json' with { type: 'json' };
import snapshot from '../src/lib/fixtures/workspace-snapshot.json' with { type: 'json' };
import backendTree from '../src/tests/backend-tree.json' with { type: 'json' };

test('nested workflow phases stay inside their card while ordinary helpers remain visible', async ({ page }) => {
    test.setTimeout(30_000);
    const chat = chats['alpha-lab'].items[0];
    const workspace = workspaces.items.find((item) => item.id === 'alpha-lab')!;
    const node = (id: string, parent: string | null, role: string, execution: string, phase: string | null, executor: boolean) => ({
        ...backendTree.items[0], subAgentId: id, chatId: chat.chat_id, parentSubAgentId: parent,
        returnsToSubAgentId: parent, role, workflowExecutionId: execution, phase,
        phaseExecutor: executor, phaseAttemptId: executor ? `${execution}:${phase}:0` : null,
        acceptsInput: executor, children: []
    });
    const nodes = [
        node('main-record', null, 'workflow-node', 'main', null, false),
        node('discovery', 'main-record', 'Discovery', 'main', 'discovery', true),
        node('branch-record', 'discovery', 'workflow-node', 'branch', null, false),
        node('proposal', 'branch-record', 'Research proposal', 'branch', 'proposal', true),
        node('critique', 'branch-record', 'Research critique', 'branch', 'critique', true),
        node('adjudicate', 'branch-record', 'Research adjudicate', 'branch', 'adjudicate', true),
        node('investigation', 'branch-record', 'Research investigation', 'branch', 'investigation', true),
        node('helper', 'discovery', 'Independent data helper', 'main', null, false)
    ];
    const handle = {
        executionId: 'branch', workflowId: 'research-investigation', version: '2.0.0',
        state: 'running', activePhases: ['investigation'],
        phases: ['proposal', 'critique', 'adjudicate', 'investigation'].map((name) => ({
            name, status: name === 'investigation' ? 'active' : 'done'
        }))
    };
    const datum = { datumType: 'workflow_execution', ok: true, structured: handle,
        text: 'Research investigation launched.', summary: 'Research investigation launched.', failure: null };
    const launch = {
        ...messages['chat-1'].items[0], message_id: 'branch-launch', role: 'tool',
        body: datum.text, kind: 'sub_agent', conversation_id: 'discovery',
        parent_conversation_id: 'main-record', execution_id: 'main', input_position: 1,
        data: { activityKind: 'delivery', subAgentId: 'discovery', sessionMessage: {
            message_id: 'branch-launch', direction: 'incoming', position: 1,
            content: { source: 'tool', text: datum.text, result: { ok: true, datums: [datum] }, requests: [], correlation: null }
        } }
    };
    const history = [{ id: launch.message_id, kind: 'message', seq: 1,
        chatId: chat.chat_id, workspaceId: workspace.id, threadId: 'discovery', ts: launch.created_at, payload: launch }];
    const phases = ['proposal', 'critique', 'adjudicate', 'investigation', 'completed', 'failed'];

    // Only external HTTP responses are scripted; the real host, timeline,
    // node projection, workflow card and scope navigation run in the browser.
    await page.addInitScript(() => localStorage.setItem('thesis.backend', 'local'));
    await page.route('**/api/**', async (route) => {
        const url = new URL(route.request().url());
        const path = url.pathname;
        if (path.endsWith('/timeline/stream')) return route.fulfill({ contentType: 'text/event-stream', body: ': idle\n\n' });
        if (path.endsWith('/timeline')) return route.fulfill({ json: {
            items: transcriptItems(url.searchParams.has('before') ? [] : history.filter((item) =>
                !url.searchParams.has('thread') || url.searchParams.get('thread') === item.threadId)),
            nextBefore: null, after: 1
        } });
        if (path.endsWith('/executions')) return route.fulfill({ json: { items: [], after: 1 } });
        if (path.endsWith('/outline')) return route.fulfill({ json: { items: [], after: 1, nextAfter: null } });
        if (path.endsWith('/sub_agents')) return route.fulfill({ json: { chatId: chat.chat_id, items: nodeSummaries(nodes) } });
        if (path.endsWith('/workflows/research-investigation')) return route.fulfill({ json: {
            workflow_id: 'research-investigation', version: '2.0.0', title: 'Research investigation', splits: {},
            phases, terminal_phases: ['completed', 'failed'], inputs: {}, routes: {
                proposal: { critique: ['critique.input'] }, critique: { adjudicate: ['adjudicate.input'] },
                adjudicate: { proposal: ['proposal.input'], investigation: ['investigation.input'], completed: ['completed.input'] }, investigation: { critique: ['critique.input'] }, completed: {}, failed: {}
            }
        } });
        if (path.endsWith('/chats')) return route.fulfill({ json: { items: [chat] } });
        if (path.endsWith(`/chats/${chat.chat_id}`)) return route.fulfill({ json: chat });
        if (path.endsWith('/workspaces')) return route.fulfill({ json: { items: [workspace] } });
        if (path.endsWith(`/workspaces/${workspace.id}`)) return route.fulfill({ json: workspace });
        if (path.endsWith('/snapshot')) return route.fulfill({ json: snapshot['alpha-lab'] });
        return route.fulfill({ status: 404, json: { detail: 'Not part of this scenario' } });
    });
    const scope = `conversation:${chat.chat_id}/sub_agent:discovery`;
    const url = `/w/${workspace.id}/conversations?chat=${chat.chat_id}&scope=${encodeURIComponent(scope)}`;
    await page.goto(url);
    const card = page.getByTestId('workflow-card').filter({ has: page.getByTestId('workflow-card-body') });
    await expect(card).toHaveCount(1);
    await expect(card.getByTestId('phase-step')).toHaveCount(4);
    await expect(card.locator('[data-phase="completed"], [data-phase="failed"]')).toHaveCount(0);
    await expect(page.getByTestId('scope-sub-agent')).toHaveCount(1);
    await expect(page.getByTestId('scope-sub-agent')).toContainText('Independent data helper');

    for (const phase of ['proposal', 'critique', 'adjudicate', 'investigation']) {
        await card.locator(`[data-testid="phase-step"][data-phase="${phase}"]`).focus();
        await page.keyboard.press('Enter');
        await expect(page.getByTestId('scope-crumb').last()).toHaveText(`Research ${phase}`);
        await expect(page.getByTestId('scope-sub-agent')).toHaveCount(0);
        await page.getByTestId('scope-back').click();
        await expect(page).toHaveURL((location) => location.searchParams.get('scope') === scope);
        await expect(page.getByTestId('workflow-card')).toHaveCount(1);
        await expect(page.getByTestId('scope-sub-agent')).toHaveCount(1);
    }
    await page.getByTestId('scope-sub-agent').click();
    await expect(page.getByTestId('scope-crumb').last()).toHaveText('Independent data helper');
    await page.getByTestId('scope-back').click();
    await page.reload();
    await expect(page.getByTestId('workflow-card')).toHaveCount(1);
    await expect(page.getByTestId('scope-sub-agent')).toHaveCount(1);
});
