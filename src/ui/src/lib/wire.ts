/** Validate canonical backend JSON once at the HTTP/SSE boundary, before UI adaptation. */
import Ajv2020 from 'ajv/dist/2020.js';
import type { ErrorObject, SchemaObject } from 'ajv';
import schema from './wire-schema.json';
import type { WireTypes } from './generated';

export class WireDecodeError extends Error {
	constructor(readonly schemaName: string, readonly path: string, readonly details: unknown) {
		// Schema paths and required names are contract metadata, never response values or dynamic object keys.
		const issues = Array.isArray(details) ? (details as ErrorObject[]).slice(0, 4).map((error) =>
			`${error.schemaPath} [${error.keyword}]${error.keyword === 'required' ? ` missing ${error.params.missingProperty}` : ''}`
		).join('; ') : '';
		super(`Invalid Thesis API response for ${schemaName} on ${path}${issues ? `: ${issues}` : ''}`);
		this.name = 'WireDecodeError';
	}
}

/** The registry must be the generated schema artifact, never an independently authored UI contract. */
export function createWireDecoder<Types extends object = Record<string, unknown>>(schema: SchemaObject) {
	const ajv = new Ajv2020({ strict: false, allErrors: true, validateFormats: false });
	const registry = { ...schema, $id: 'urn:fca:thesis:wire' };
	ajv.addSchema(registry);
	const validators = new Map<string, ReturnType<typeof ajv.compile>>();
	const validateRecord = (schemaName: string, value: unknown, path: string): void => {
		let validate = validators.get(schemaName);
		if (!validate) {
			if (!(schemaName in (registry.$defs ?? {}))) throw new Error(`Backend schema is missing: ${schemaName}`);
			validate = ajv.compile({ $ref: `urn:fca:thesis:wire#/$defs/${schemaName}` });
			validators.set(schemaName, validate);
		}
		if (!validate(value)) throw new WireDecodeError(schemaName, path, validate.errors);
	};
	return <Name extends keyof Types & string>(schemaName: Name, value: unknown, path: string): Types[Name] => {
		validateRecord(schemaName, value, path);
		// Event payloads are extension objects, but embedded tool outcomes have one canonical schema.
		const events = schemaName === 'ExecutionEventWire' ? [value]
			: schemaName === 'TimelineItemWire' ? [(value as { kind: string; payload: unknown }).kind === 'event' ? (value as { payload: unknown }).payload : null]
			: schemaName === 'TimelinePageWire' ? (value as { items: { kind: string; payload: unknown }[] }).items.filter((item) => item.kind === 'event').map((item) => item.payload)
			: [];
		for (const event of events) {
			if (!event) continue;
			const payload = (event as { payload: Record<string, unknown> }).payload;
			const tool = payload.toolResult ?? payload.tool_result;
			if (tool != null) validateRecord('ToolResultWire', tool, path);
		}
		return value as Types[Name];
	};
}

export const decodeWire = createWireDecoder<WireTypes>(schema);
