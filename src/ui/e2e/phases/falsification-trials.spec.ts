import {
	driveToTrials,
	expect,
	phaseStep,
	test
} from '../fixtures';

/**
 * Falsification-trials phase, live end to end: after model creation is
 * approved, this deterministic phase runs the research model through the
 * harness (data ensure + study). The scope renders the read-only run view —
 * live progress lines and one result or error card — and never a composer.
 *
 * The run can take many minutes; this spec asserts the live run surface and
 * fails if the phase records an error, because a failed trial run is a real
 * product finding, not a UI detail.
 */
test.describe('falsification trials phase', () => {
	test.setTimeout(1_800_000);

	test('runs deterministically with a live run view and no composer', async ({
		chatPage: page
	}) => {
		await driveToTrials(page);

		await phaseStep(page, 'falsification_trials').click();
		await expect(page.getByTestId('phase-run-view')).toBeVisible({ timeout: 60_000 });
		await expect(page.getByTestId('sub-agent-composer')).toHaveCount(0);
		await expect(page.getByTestId('scope-readonly')).toContainText('deterministic');
		await expect(page.getByTestId('phase-run-status')).toHaveText(
			/Running|Completed|Failed/,
			{ timeout: 120_000 }
		);

		// Wait for the first observable outcome: a progress line, a result, or
		// an error. A run that stays silent past this window fails the test.
		await expect(
			page
				.locator(
					'[data-testid="phase-run-error"], [data-testid="phase-progress-line"], [data-testid="phase-run-result"]'
				)
				.first()
		).toBeVisible({ timeout: 300_000 });
		if (await page.getByTestId('phase-run-error').isVisible()) {
			throw new Error(
				`falsification trials failed: ${await page
					.getByTestId('phase-run-error-message')
					.innerText()}`
			);
		}
	});
});
