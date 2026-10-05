import {
	answerPhase,
	backToRoot,
	expect,
	launchWorkflow,
	openPhase,
	phaseStep,
	test,
	waitForPhaseAttention
} from './fixtures';

/**
 * The full hypothesis-development journey through the live UX, up to the
 * deterministic falsification trials (the strategy phases that follow the
 * verdict are out of scope until they are implemented properly):
 *
 *   launch -> discovery proposal -> operator confirmation
 *          -> model-creation proposal -> operator confirmation
 *          -> falsification trials run view.
 *
 * The per-phase surfaces are also covered by the focused specs under
 * `e2e/phases/`; this spec is the cross-phase smoke that the handoffs work.
 */
test.describe('hypothesis development workflow', () => {
	test.setTimeout(1_800_000);

	test('launch, confirm discovery and model creation, and watch the trials run', async ({
		chatPage: page
	}) => {
		await launchWorkflow(page);

		// Discovery: the agent runs its targeted studies, then proposes and
		// asks for confirmation through the wrapped input tool.
		await waitForPhaseAttention(page, 'discovery', 900_000);
		await openPhase(page, 'discovery');
		// Study execution is mandatory UX: the studies behind the proposal must
		// surface as scoped tool activity, not hidden work.
		await expect(page.locator('[data-testid="scope-tool"]').first()).toBeVisible({
			timeout: 300_000
		});
		await answerPhase(page, 'Confirmed — carry this candidate into model creation.');
		await backToRoot(page);

		// Model creation: author a study, compile-check it, propose, and ask.
		await waitForPhaseAttention(page, 'model_creation', 1_200_000);
		await openPhase(page, 'model_creation');
		// The only harness tool in this phase is validate_study, so a tool card
		// here is the compile check surfacing in the UX.
		await expect(page.locator('[data-testid="scope-tool"]').first()).toBeVisible({
			timeout: 600_000
		});
		await answerPhase(page, 'Approved — run the falsification trials.');
		await backToRoot(page);

		// Falsification trials: a deterministic run view with no composer.
		await expect(phaseStep(page, 'falsification_trials')).toBeEnabled({
			timeout: 1_200_000
		});
		await phaseStep(page, 'falsification_trials').click();
		await expect(page.getByTestId('phase-run-view')).toBeVisible({ timeout: 60_000 });
		await expect(page.getByTestId('phase-run-status')).toHaveText(
			/Running|Completed|Failed/,
			{ timeout: 120_000 }
		);
		await expect(
			page.locator('[data-testid="phase-run-log"], [data-testid="phase-run-empty"]')
		).toBeVisible();
		await expect(page.getByTestId('sub-agent-composer')).toHaveCount(0);
	});
});
