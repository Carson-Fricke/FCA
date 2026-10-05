import { describe, expect, it } from 'vitest';
import { generateWire } from '../../generate-wire.mjs';
import { createWireDecoder, WireDecodeError } from '$lib/wire';

// A synthetic schema tests generator mechanics, not a claim about backend fields.
const schemas = {
	Example: { type: 'object', required: ['count'], properties: { count: { type: 'integer' } }, additionalProperties: false },
	Envelope: { type: 'object', required: ['item'], properties: { item: { $ref: '#/components/schemas/Example' } } }
};

describe('backend schema generation seam', () => {
	it('generates deterministically regardless of definition order and resolves references', async () => {
		const first = await generateWire([{ components: { schemas } }]);
		const second = await generateWire([{ components: { schemas: { Envelope: schemas.Envelope, Example: schemas.Example } } }]);
		expect(second).toEqual(first);
		expect(first.types).toContain('count: number');
		expect(first.types.match(/export interface Example\b/g)).toHaveLength(1);
		expect(first.types.match(/export interface Envelope\b/g)).toHaveLength(1);
		const decode = createWireDecoder(JSON.parse(first.schema));
		expect(decode('Envelope', { item: { count: 2 } }, '/example')).toEqual({ item: { count: 2 } });
		expect(() => decode('Envelope', { item: { count: '2' } }, '/example')).toThrow(WireDecodeError);
		expect(() => decode('Envelope', { item: {} }, '/example')).toThrow(WireDecodeError);
	});
	it('fails on absent and duplicate sources rather than inventing a contract', async () => {
		await expect(generateWire([{}])).rejects.toThrow('Backend export');
		await expect(generateWire([{ openapi: '3.0.3', components: { schemas } }])).rejects.toThrow('OpenAPI 3.1');
		await expect(generateWire([{ $defs: schemas }, { $defs: schemas }])).rejects.toThrow('Duplicate');
	});
	it('preserves properties named title and compiles repeated property titles without alias collisions', async () => {
		const result = await generateWire([{ $defs: {
			First: { type: 'object', properties: { title: { type: 'string', title: 'Title' }, items: { type: 'array', title: 'Items', items: { type: 'string' } } } },
			Second: { type: 'object', properties: { items: { type: 'array', title: 'Items', items: { type: 'number' } } } }
		} }]);
		expect(result.types).toContain('title?: string');
		expect(result.types).toContain('items?: string[]');
		expect(result.types).toContain('items?: number[]');
	});
});
