import {
	expect,
	test as base,
	type Page
} from '@playwright/test';

/**
 * E2E against the real local stack: the running Thesis API (port 8000,
 * restarted on current code) and the Vite dev server on 5173 (same-origin
 * proxy, bearer injected server-side). The stack is started externally via
 * `scripts/local/start-stack.ps1`; these tests assume it is up.
 *
 * The workflow is live-model driven, so timing is generous and every assertion
 * targets a durable UI outcome. Each test creates its own chat. Tests that
 * launch a workflow stop its execution at teardown on a best-effort basis so
 * an ended test cannot leave the model or harness working indefinitely.
 */

export const WORKSPACE_ID = 'test-ws-1';
export const CONVERSATIONS_URL = `/w/${WORKSPACE_ID}/conversations`;

export const test = base.extend<{ chatPage: Page }>({
	chatPage: async ({ page }, use) => {
		// A fresh browser profile has no stored backend choice, and the app
		// resolves to fixture mode without one - seed the local backend before
		// the app loads so `$lib/api` talks to the real stack.
		await page.addInitScript(() => {
			localStorage.setItem('thesis.backend', 'local');
		});
		await page.goto(CONVERSATIONS_URL);
		await expect(page.getByTestId('chat-surface')).toBeVisible();
		// Start from a brand-new conversation every time, so runs are
		// independent and re-runnable.
		await page.getByTestId('new-conversation').click();
		await expect(page.getByTestId('chat-composer')).toBeVisible();
		await use(page);
		await stopLaunchedWorkflow(page);
	}
});

export { expect };

/** Best-effort teardown: stop the workflow this test launched. */
async function stopLaunchedWorkflow(page: Page): Promise<void> {
	try {
		const execution = await page
			.getByTestId('workflow-card')
			.first()
			.getAttribute('data-execution');
		if (!execution) return;
		const origin = new URL(page.url()).origin;
		await page.request.post(
			`${origin}/api/v1/workspaces/${WORKSPACE_ID}/executions/${encodeURIComponent(execution)}/stop`,
			{ data: { actor_id: 'operator' } }
		);
	} catch {
		// A finished or unreachable run must not fail the test teardown.
	}
}

/** Open the composer, type, send. */
export async function send(page: Page, body: string): Promise<void> {
	await page.locator('[data-testid="chat-composer"] textarea').fill(body);
	await page.getByTestId('composer-send').click();
}

/** The workflow card for the launch this test created. */
export function workflowCard(page: Page) {
	return page.getByTestId('workflow-card');
}

/** One phase node in the workflow card's transition graph. */
export function phaseStep(page: Page, phase: string) {
	return page.locator(`[data-testid="phase-step"][data-phase="${phase}"]`);
}

/** Ask the workspace agent to start the hypothesis-development workflow. */
export async function launchWorkflow(page: Page): Promise<void> {
	await send(
		page,
		'I think SPY momentum persists over the last month. Start the hypothesis development workflow.'
	);
	await expect(workflowCard(page)).toBeVisible({ timeout: 180_000 });
	await expect(page.getByTestId('workflow-card-status')).toHaveText(
		/running|started|launched/,
		{ timeout: 180_000 }
	);
}

/** Wait until a phase agent has paused on operator input. */
export async function waitForPhaseAttention(
	page: Page,
	phase: string,
	timeout = 900_000
): Promise<void> {
	await expect(phaseStep(page, phase)).toHaveAttribute('data-attention', 'true', {
		timeout
	});
	await expect(page.getByTestId('workflow-card-needs-input')).toContainText(phase);
}

/** Wait until a phase node is spawned and enterable (not pending). */
export async function waitForPhaseRunnable(
	page: Page,
	phase: string,
	timeout = 900_000
): Promise<void> {
	await expect(phaseStep(page, phase)).toBeEnabled({ timeout });
}

/** Enter a phase's scope from the workflow card. */
export async function openPhase(page: Page, phase: string): Promise<void> {
	await phaseStep(page, phase).click();
	await expect(page.getByTestId('scope-stack')).toBeVisible({ timeout: 60_000 });
}

/** Answer the asking phase agent and confirm the send was accepted. */
export async function answerPhase(page: Page, body: string): Promise<void> {
	await expect(page.getByTestId('operator-question-card').last()).toBeVisible({
		timeout: 180_000
	});
	await expect(page.getByTestId('sub-agent-composer')).toBeVisible();
	await page.getByTestId('sub-agent-composer-input').fill(body);
	await page.getByTestId('sub-agent-composer-send').click();
	// The draft clears only after the backend accepted the answer; a rejected
	// send keeps the draft and shows the error instead.
	await expect(page.getByTestId('sub-agent-composer-input')).toHaveValue('', {
		timeout: 180_000
	});
}

/** Leave the phase scope and return to the root transcript. */
export async function backToRoot(page: Page): Promise<void> {
	await page.getByTestId('scope-back').click();
	await expect(workflowCard(page)).toBeVisible({ timeout: 60_000 });
}

/** Wait for a phase's input request, open it, answer, and return to root. */
export async function confirmPhase(
	page: Page,
	phase: string,
	body: string,
	attentionTimeout = 1_200_000
): Promise<void> {
	await waitForPhaseAttention(page, phase, attentionTimeout);
	await openPhase(page, phase);
	await answerPhase(page, body);
	await backToRoot(page);
}

/** Drive the live workflow through discovery and model creation to trials. */
export async function driveToTrials(page: Page): Promise<void> {
	await launchWorkflow(page);
	await confirmPhase(page, 'discovery', 'Confirmed.');
	await confirmPhase(page, 'model_creation', 'Approved.', 1_500_000);
	await waitForPhaseRunnable(page, 'falsification_trials', 1_200_000);
}
