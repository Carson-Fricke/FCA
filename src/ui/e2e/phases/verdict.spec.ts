import {
	confirmPhase,
	expect,
	launchWorkflow,
	openPhase,
	test,
	waitForPhaseAttention
} from '../fixtures';

/**
 * Verdict phase, live end to end: after the deterministic falsification
 * trials complete, the verdict agent is spawned with the trial findings. It
 * analyzes the evidence and pauses for the operator through
 * `request_workflow_input` before requesting the phase transition.
 *
 * This spec deliberately does not answer the verdict question: completing the
 * verdict enters the strategy phases, which are out of scope until they are
 * implemented properly. It verifies that the workflow reaches the verdict
 * surface with a live, answerable agent.
 */
test.describe('verdict phase', () => {
	test.setTimeout(2_700_000);

	test('reaches the verdict agent and asks the operator before completing', async ({
		chatPage: page
	}) => {
		await launchWorkflow(page);
		await confirmPhase(page, 'discovery', 'Confirmed.');
		await confirmPhase(page, 'model_creation', 'Approved.', 1_500_000);

		// The trials phase must finish before the verdict agent is spawned.
		await waitForPhaseAttention(page, 'verdict', 2_400_000);
		await openPhase(page, 'verdict');

		await expect(page.getByTestId('scope-transcript')).toBeVisible();
		await expect(page.getByTestId('operator-question-card').last()).toBeVisible({
			timeout: 180_000
		});
		await expect(page.getByTestId('sub-agent-composer')).toBeVisible();
	});
});
