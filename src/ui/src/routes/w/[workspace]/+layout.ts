import { error } from '@sveltejs/kit';
import type { LayoutLoad } from './$types';
import { getSnapshot } from '$lib/api';
import type { WorkspaceSnapshot } from '$lib/types';

export const load: LayoutLoad = async ({ params }): Promise<{ workspaceId: string; snapshot: WorkspaceSnapshot }> => {
	try {
		const snapshot = await getSnapshot(params.workspace);
		return { workspaceId: params.workspace, snapshot };
	} catch {
		error(404, `Workspace “${params.workspace}” is not available for this session.`);
	}
};
