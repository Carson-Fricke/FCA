import {
	answerPhase,
	backToRoot,
	expect,
	launchWorkflow,
	openPhase,
	test,
	waitForPhaseAttention,
	waitForPhaseRunnable
} from '../fixtures';

/**
 * Discovery phase, live end to end: the agent runs its targeted studies on the
 * operator's premise (study tool activity must surface in the scoped UX), then
 * proposes one candidate and pauses on `request_workflow_input`. Confirming the
 * proposal is what advances the workflow to model creation.
 */
test.describe('discovery phase', () => {
	test.setTimeout(1_200_000);

	test('runs studies, proposes, and accepts the operator confirmation', async ({
		chatPage: page
	}) => {
		await launchWorkflow(page);

		await waitForPhaseAttention(page, 'discovery', 900_000);
		await openPhase(page, 'discovery');
		await expect(page.getByTestId('scope-transcript')).toBeVisible();
		// Discovery is study-driven: at least one study tool call/result must be
		// visible before the agent asks for confirmation.
		await expect(page.locator('[data-testid="scope-tool"]').first()).toBeVisible({
			timeout: 300_000
		});
		await expect(page.getByTestId('operator-question-card').last()).toBeVisible();
		await expect(page.getByTestId('sub-agent-composer')).toBeVisible();

		await answerPhase(page, 'Confirmed — carry this candidate into model creation.');
		await backToRoot(page);

		// The confirmation advances the workflow: model creation becomes
		// runnable from the transition graph.
		await waitForPhaseRunnable(page, 'model_creation', 600_000);
	});
});
