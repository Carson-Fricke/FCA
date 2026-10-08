import { fireEvent, render, screen, within } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getArtifactFile, listWorkspaceArtifacts } from '$lib/api';
import { fixtures } from '$lib/fixtures';
import ArtifactsPage from '../routes/w/[workspace]/artifacts/+page.svelte';

vi.mock('$lib/api', () => ({
	listWorkspaceArtifacts: vi.fn(),
	getArtifactFile: vi.fn()
}));

beforeEach(() => {
	vi.mocked(listWorkspaceArtifacts).mockReset();
	vi.mocked(getArtifactFile).mockReset();
});

function renderPage(workspaceId: string) {
	const catalog = fixtures.workspaceArtifacts[workspaceId];
	expect(catalog).toBeDefined();
	vi.mocked(listWorkspaceArtifacts).mockResolvedValue(catalog);
	vi.mocked(getArtifactFile).mockImplementation((_workspace, artifactId, path) =>
		Promise.resolve(
			fixtures.artifactFiles[`${artifactId}|${path}`] ?? {
				path,
				text: `// ${path}`,
				truncated: false,
				sha256: '0'.repeat(64),
				size_bytes: 0
			}
		)
	);
	return render(ArtifactsPage, {
		props: {
			data: {
				workspaceId,
				workspace: fixtures.workspaces.items.find((workspace) => workspace.id === workspaceId)!
			}
		}
	});
}

describe('workspace artifacts', () => {
	it.each(['alpha-lab', 'beta-quant'])('renders the durable catalog for %s', async (workspaceId) => {
		renderPage(workspaceId);
		expect(await screen.findByRole('heading', { name: 'Workspace artifacts' })).toBeInTheDocument();
		expect(listWorkspaceArtifacts).toHaveBeenCalledWith(workspaceId);
		const catalog = fixtures.workspaceArtifacts[workspaceId];
		expect(await screen.findAllByTestId('artifact-card')).toHaveLength(catalog.items.length);
		for (const item of catalog.items) {
			expect(screen.getByRole('heading', { name: item.title })).toBeInTheDocument();
		}
	});

	it('opens the explorer and reads a committed file', async () => {
		renderPage('alpha-lab');
		await screen.findAllByTestId('artifact-card');
		await fireEvent.click(screen.getAllByTestId('artifact-card')[0]);
		expect(await screen.findByTestId('artifact-detail')).toBeInTheDocument();
		expect(screen.getByTestId('strategy-preview')).toBeInTheDocument();
		expect(screen.getByText(/no recorded run/i)).toBeInTheDocument();
		expect(getArtifactFile).toHaveBeenCalledWith(
			'alpha-lab',
			'strategy:momentum-breakout',
			'src/lib.rs'
		);
		expect(await screen.findByText('export_strategy_program!(MomentumBreakout);')).toBeInTheDocument();
	});

	it('links indicator usage into the study artifact', async () => {
		renderPage('alpha-lab');
		const cards = await screen.findAllByTestId('artifact-card');
		const indicatorCard = cards.find((card) =>
			card.textContent?.includes('rsi_value')
		);
		expect(indicatorCard).toBeDefined();
		await fireEvent.click(indicatorCard!);
		expect(await screen.findByTestId('indicator-preview')).toBeInTheDocument();
		expect(screen.getByText('no recorded series')).toBeInTheDocument();
		const usage = within(screen.getByTestId('indicator-preview'));
		await fireEvent.click(usage.getByRole('button', { name: /breakout_thresholds/ }));
		expect(await screen.findByTestId('study-preview')).toBeInTheDocument();
	});

	it('filters the gallery by artifact kind', async () => {
		renderPage('alpha-lab');
		await screen.findAllByTestId('artifact-card');
		await fireEvent.click(screen.getByRole('button', { name: /^Studies/ }));
		const cards = screen.getAllByTestId('artifact-card');
		expect(cards).toHaveLength(1);
		expect(cards[0].textContent).toContain('breakout_thresholds');
	});
});
