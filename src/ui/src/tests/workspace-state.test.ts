import { render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getWorkspaceState } from '$lib/api';
import { fixtures } from '$lib/fixtures';
import WorkspaceStatePage from '../routes/w/[workspace]/workspace-state/+page.svelte';

vi.mock('$lib/api', () => ({ getWorkspaceState: vi.fn() }));

beforeEach(() => { vi.mocked(getWorkspaceState).mockReset(); });

describe('workspace state', () => {
	it.each(['alpha-lab', 'beta-quant'])('renders the retained state for %s', async (workspaceId) => {
		const state = fixtures.workspaceState[workspaceId];
		expect(Object.keys(state).sort()).toEqual(['consentAudit', 'evidence', 'lineages']);
		vi.mocked(getWorkspaceState).mockResolvedValue(state);
		render(WorkspaceStatePage, { props: { data: {
			workspaceId,
			workspace: fixtures.workspaces.items.find((workspace) => workspace.id === workspaceId)!
		} } });
		expect(await screen.findByRole('heading', { name: 'Strategy lineage' })).toBeInTheDocument();
		expect(screen.getByRole('heading', { name: 'Evidence' })).toBeInTheDocument();
		expect(screen.getByRole('heading', { name: 'Consent & audit' })).toBeInTheDocument();
		expect(screen.getByText(state.evidence[0].title)).toBeInTheDocument();
		expect(getWorkspaceState).toHaveBeenCalledWith(workspaceId);
	});
});
