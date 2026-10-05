import {
	confirmPhase,
	expect,
	launchWorkflow,
	openPhase,
	test,
	waitForPhaseAttention,
	waitForPhaseRunnable
} from '../fixtures';

/**
 * Model-creation phase, live end to end: after discovery is confirmed, the
 * agent authors a Rust study program, compile-checks it through the harness
 * (`validate_study`, the only harness tool this phase holds), proposes the
 * model, and pauses for the operator. Approval runs the completion-time
 * contract: the engine validates the study and hands off to the deterministic
 * falsification trials.
 */
test.describe('model creation phase', () => {
	test.setTimeout(1_800_000);

	test('compile-checks the authored study and hands off to trials', async ({
		chatPage: page
	}) => {
		await launchWorkflow(page);
		await confirmPhase(page, 'discovery', 'Confirmed.');

		await waitForPhaseAttention(page, 'model_creation', 1_500_000);
		await openPhase(page, 'model_creation');
		await expect(page.getByTestId('scope-transcript')).toBeVisible();
		// `validate_study` is the only harness tool in this phase, so a tool card
		// here is the compile check surfacing in the UX.
		await expect(page.locator('[data-testid="scope-tool"]').first()).toBeVisible({
			timeout: 600_000
		});
		await expect(page.getByTestId('operator-question-card').last()).toBeVisible();
		await expect(page.getByTestId('sub-agent-composer')).toBeVisible();

		await page
			.getByTestId('sub-agent-composer-input')
			.fill('Approved — run the falsification trials.');
		await page.getByTestId('sub-agent-composer-send').click();
		await expect(page.getByTestId('sub-agent-composer-input')).toHaveValue('', {
			timeout: 180_000
		});
		await page.getByTestId('scope-back').click();
		await expect(page.getByTestId('workflow-card')).toBeVisible({ timeout: 60_000 });

		// Approval validates the study on the completion path and spawns the
		// deterministic trials phase.
		await waitForPhaseRunnable(page, 'falsification_trials', 1_200_000);
	});
});
