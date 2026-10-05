import { error } from '@sveltejs/kit';
import type { LayoutLoad } from './$types';
import { getWorkspace } from '$lib/api';
import type { WorkspaceSummary } from '$lib/types';

/**
 * Render gate for a workspace route. It loads only the workspace summary so
 * the shell renders without waiting for counts, activity, or the workspace
 * catalog. Those load after render from the layout component.
 */
export const load: LayoutLoad = async ({
	params
}): Promise<{ workspaceId: string; workspace: WorkspaceSummary }> => {
	try {
		const workspace = await getWorkspace(params.workspace);
		return { workspaceId: params.workspace, workspace };
	} catch {
		error(404, `Workspace “${params.workspace}” is not available for this session.`);
	}
};
