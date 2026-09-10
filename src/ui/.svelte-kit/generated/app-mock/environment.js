// Test-only mock of the SvelteKit runtime modules so vitest can import
// modules that use `$app/*` imports without a full SvelteKit build.
export const browser = typeof window !== 'undefined';
export const dev = false;
export const version = 'test';
