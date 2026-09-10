import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/svelte';
import { afterEach, beforeAll, vi } from 'vitest';

afterEach(() => cleanup());

beforeAll(() => {
	// Node's jsdom-less localStorage warning path: localStorage is exposed
	// but non-functional without --localstorage-file; tests need a working
	// in-memory shim for the cursor/fixture persistence surfaces.
	const memory = new Map<string, string>();
	vi.stubGlobal('localStorage', {
		getItem: (key: string) => (memory.has(key) ? (memory.get(key) as string) : null),
		setItem: (key: string, value: string) => void memory.set(key, value),
		removeItem: (key: string) => void memory.delete(key),
		clear: () => memory.clear()
	});

	// jsdom lacks ResizeObserver and canvas 2D support; the chart components
	// degrade to no-ops under these stubs (no drawing attempted in tests).
	vi.stubGlobal(
		'ResizeObserver',
		class {
			observe() {}
			unobserve() {}
			disconnect() {}
		}
	);

	const noop = () => {};
	const gradient = { addColorStop: noop };
	const ctx = {
		setTransform: noop,
		fillRect: noop,
		beginPath: noop,
		moveTo: noop,
		lineTo: noop,
		stroke: noop,
		fill: noop,
		fillText: noop,
		measureText: () => ({ width: 10 }),
		createLinearGradient: () => gradient,
		strokeRect: noop,
		arcTo: noop,
		closePath: noop,
		save: noop,
		restore: noop
	};
	HTMLCanvasElement.prototype.getContext = vi.fn(() => ctx) as unknown as typeof HTMLCanvasElement.prototype.getContext;
});
