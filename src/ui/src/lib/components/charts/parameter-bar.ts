import type { VariationRunData } from '$lib/transcript';

/** One X-axis level of a parameter chart with both metric values per level. */
export interface ParameterBarCategory {
	key: string;
	label: string;
	value: string | number | boolean;
	center: boolean;
	state: string | null;
	jobId: string | null;
	error: string | null;
	totalReturn: number | null;
	profitFactor: number | null;
 totalReturnGap?: string;
 profitFactorGap?: string;
}

/** Read a finite metric by its canonical key, tolerating an alternate casing. */
export function metricValue(
	metrics: Record<string, number | null> | null | undefined,
	...keys: string[]
): number | null {
	if (!metrics) return null;
	for (const key of keys) {
		const value = metrics[key];
		if (typeof value === 'number' && Number.isFinite(value)) return value;
	}
	return null;
}

/** Harness job states; anything else stays visually distinct as unknown. */
export const SUCCEEDED_STATES = ['succeeded'] as const;
export const FAILED_STATES = ['failed', 'cancelled'] as const;
export const ACTIVE_STATES = ['queued', 'running'] as const;

/** Shared run-state tone so chart markers and summary rows agree. */
export function runStateTone(state: string | null | undefined): string {
	const normalized = (state ?? '').toLowerCase();
	if ((SUCCEEDED_STATES as readonly string[]).includes(normalized)) return 'text-profit';
	if ((FAILED_STATES as readonly string[]).includes(normalized)) return 'text-loss';
	if ((ACTIVE_STATES as readonly string[]).includes(normalized)) return 'text-accent';
	return 'text-warn';
}

/** Whether a run state is a success; absent states are not successes. */
export function isSucceededState(state: string | null | undefined): boolean {
	return (SUCCEEDED_STATES as readonly string[]).includes((state ?? '').toLowerCase());
}

/**
 * Build the chart categories for one varied parameter.
 *
 * The center configuration is the selected baseline category; points that
 * leave the parameter at the center value (they vary a different key) are not
 * plotted on this parameter's axis. Numeric levels order numerically; mixed or
 * categorical levels keep first-seen order with the center first.
 */
export function parameterCategories(
	parameter: string,
	center: VariationRunData | null,
	points: VariationRunData[]
): ParameterBarCategory[] {
	const categories = new Map<string, ParameterBarCategory>();
	const add = (value: string | number | boolean, run: VariationRunData, isCenter: boolean): void => {
		const key = run.levelIdentities[parameter];
        if (!key) return;
		if (categories.has(key)) return;
		categories.set(key, {
			key,
			label: run.levelLabels[parameter] ?? String(value),
			value,
			center: isCenter,
			state: run.state,
			jobId: run.jobId,
			error: run.error,
			totalReturn: metricValue(run.metrics, 'total_return', 'netTotalReturn', 'totalReturn'),
			profitFactor: metricValue(run.metrics, 'profit_factor', 'profitFactor'),
            totalReturnGap: run.metricReasons?.['total_return'] ? `undefined: ${run.metricReasons['total_return']}` : run.state === 'failed' ? 'failed' : !run.jobId ? 'untested' : 'missing',
            profitFactorGap: run.metricReasons?.['profit_factor'] ? `undefined: ${run.metricReasons['profit_factor']}` : run.state === 'failed' ? 'failed' : !run.jobId ? 'untested' : 'missing'
		});
	};
	const baseline = center?.parameters[parameter];
	if (center && baseline !== undefined) add(baseline, center, true);
	for (const point of points) {
		const value = point.parameters[parameter];
		if (value === undefined) continue;
        if (!point.levelIdentities[parameter]) continue;
        if (center && Object.keys(point.parameters).some((key) => key !== parameter && point.levelIdentities[key] !== center.levelIdentities[key])) continue;
		if (center && baseline !== undefined && center.levelIdentities[parameter] === point.levelIdentities[parameter]) continue;
		add(value, point, false);
	}
	const ordered = [...categories.values()];
	if (ordered.length > 1 && ordered.every((category) => typeof category.value === 'number')) {
		ordered.sort((a, b) => (a.value as number) - (b.value as number));
	}
	return ordered;
}
