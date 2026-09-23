import { expect, send, test, workflowCard } from './fixtures';

/**
 * The hypothesis-development workflow, end to end, through the live UX:
 * launch -> creation proposal -> operator confirmation -> trials run view ->
 * verdict phase. The model is live, so timing is generous and every
 * assertion targets a durable UI outcome.
 */
test.describe('hypothesis development workflow', () => {
	// The trials phase runs the research model through the harness (data
	// ensure + study), which can take several minutes.
	test.setTimeout(900_000);

	test('launch, confirm the proposal, and watch the trials phase run', async ({
		chatPage
	}) => {
		const page = chatPage;

		// Launch: the creation agent proposes, then asks for confirmation
		// through the wrapped input tool (the confirmation protocol).
		await send(
			page,
			'I think SPY momentum persists over the last month. Start the hypothesis development workflow.'
		);
		await expect(workflowCard(page)).toBeVisible({ timeout: 120_000 });
		await expect(
			page.getByTestId('workflow-card-status')
		).toHaveText(/running|started|launched/, { timeout: 120_000 });

		// The creation agent proposes, then asks for confirmation through the
		// wrapped input tool. The question itself lives on the node's scope
		// (root shows the card's attention line - the ask is on the agent).
		await expect(page.getByTestId('workflow-card-needs-input')).toBeVisible({
			timeout: 300_000
		});

		// Drill into the current phase and answer the agent.
		await page.getByTestId('workflow-card-body').click();
		await expect(page.getByTestId('scope-transcript')).toBeVisible({ timeout: 30_000 });
		await expect(page.getByTestId('operator-question-card')).toBeVisible({
			timeout: 60_000
		});
		await expect(page.getByTestId('sub-agent-composer')).toBeVisible();
		await page.getByTestId('sub-agent-composer-input').fill('I confirm');
		await page.getByTestId('sub-agent-composer-send').click();

		// Back out to the root: the workflow advances through the deterministic
		// falsification-trials phase into verdict.
		await expect(page.getByTestId('scope-back')).toBeVisible();
		await page.getByTestId('scope-back').click();
		await expect(workflowCard(page)).toBeVisible();

		// The falsification_trials pill becomes enterable once the cascade
		// reaches the verdict phase; the trials pill is a run view, not a chat.
		const trialsPill = page.locator('[data-testid="phase-step"][data-phase="falsification_trials"]');
		await expect(trialsPill).toBeVisible({ timeout: 600_000 });
		await trialsPill.click();
		await expect(page.getByTestId('phase-run-view')).toBeVisible({ timeout: 30_000 });

		// The run view is the trials surface: a live progress log while the
		// research model runs through the harness, and the result (or error)
		// card when it completes - never a composer. The study itself can run
		// for many minutes, so the e2e asserts the live monitoring surface;
		// the result/error renderings are covered by the PhaseRunView
		// component tests.
		await expect(
			page.locator('[data-testid="phase-run-log"], [data-testid="phase-run-empty"]')
		).toBeVisible();
		await expect(page.getByTestId('phase-progress-line').first()).toBeVisible({
			timeout: 120_000
		});
		await expect(page.getByTestId('sub-agent-composer')).toHaveCount(0);
		await page.getByTestId('scope-back').click();
	});
});
