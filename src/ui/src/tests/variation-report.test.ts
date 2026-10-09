import { fireEvent, render, screen, within } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import VariationReportCard from '$lib/components/chat/VariationReportCard.svelte';
import { buildNodeEntries, decodeVariationReport } from '$lib/transcript';
import fixture from './fixtures/research-reports.json';
import ScopeTranscript from '$lib/components/chat/ScopeTranscript.svelte';

describe('accepted research evidence reports', () => {
  it('keeps integer and float assignments distinct in charts and matched forward comparisons', () => {
    render(VariationReportCard, { props: { data: decodeVariationReport(fixture.typedIdentity)! } });
    const rows = within(screen.getByTestId('variation-chart-fast')).getAllByTestId('parameter-bar-row');
    expect(rows.map((row) => row.dataset.level)).toContain('5');
    expect(rows.map((row) => row.dataset.level)).toContain('5.0');
    const evidenceRows = screen.getAllByTestId('research-evidence-row');
    const decimal = evidenceRows.find((row) => row.dataset.parameterIdentity === fixture.typedIdentity.points.at(-1)!.parameterIdentity)!;
    expect(decimal).toHaveTextContent('fast=5.0');
    expect(decimal).toHaveTextContent('0.22 / 2.2');
    const integer = evidenceRows.find((row) => row.dataset.parameterIdentity === fixture.typedIdentity.center.parameterIdentity)!;
    expect(integer).toHaveTextContent('0.12 / 1.6');
  });
  it('opens the canonical job result and artifact manifest on request', async () => {
    render(VariationReportCard, { props: { data: decodeVariationReport(fixture.development)! } });
    await fireEvent.click(screen.getByRole('button', {name:'Inspect job and artifacts: strategy-momentum-scope-run-1'}));
    expect(await screen.findByText('No artifacts published.')).toBeInTheDocument();
  });
  it('shows declared joint assignment interactions without mixing them into marginal curves', () => {
    render(VariationReportCard, { props: { data: decodeVariationReport(fixture.joint)! } });
    expect(screen.getByTestId('research-interaction-chart')).toHaveTextContent('fast=6, slow=21');
    expect(within(screen.getByTestId('variation-chart-fast')).getAllByTestId('parameter-bar-row')).toHaveLength(3);
  });
  it('renders backend decimal metrics, states, and undefined reasons without inventing zeroes', () => {
    render(VariationReportCard, { props: { data: decodeVariationReport(fixture.development)! } });
    const fast = screen.getByTestId('variation-chart-fast');
    expect(fast).toHaveTextContent('0.1200');
    expect(fast).toHaveTextContent('undefined: no_losing_trades');
    const slow = screen.getByTestId('variation-chart-slow');
    expect(slow).toHaveTextContent('failed');
    expect(slow).toHaveTextContent('running');
    const link = screen.getByTestId('research-export');
    const exported = JSON.parse(decodeURIComponent(link.getAttribute('href')!.split(',')[1]));
    expect(exported).toEqual(fixture.development);
    expect(within(screen.getByTestId('research-evidence-table')).getByText('strategy-momentum-scope-var-3 · failed')).toBeInTheDocument();
  });
  it('uses the same tables on forward data with matched development values and full evidence export', () => {
    render(VariationReportCard, { props: { data: decodeVariationReport(fixture.forward)! } });
    expect(screen.getByText('Forward Parameter Variation')).toBeInTheDocument();
    expect(screen.getByTestId('research-comparison')).toHaveTextContent('Development Sharpe');
    expect(screen.getByTestId('research-evidence-table')).toHaveTextContent('Development return / profit factor');
    expect(screen.getByTestId('research-evidence-table')).toHaveTextContent('0.12 / 1.6');
    expect(screen.getByTestId('variation-chart-fast')).toHaveTextContent('0.0500');
  });
  it('keeps accepted artifact data through transcript transport', () => {
    const entries = buildNodeEntries({ messages: [], events: [{actorId:'thesis-workflow',actorScope:'workflow',correlationId:'exec-1',cursor:1,eventId:'evt-1',eventType:'sub_agent.output',executionId:'exec-1',intentEpoch:0,lateResult:false,occurredAt:'2026-01-01T00:00:00Z',payload:{text:'Report',artifact:fixture.forward},workspaceId:'ws-1'}] });
    render(ScopeTranscript, { props: { entries, workspaceId:'ws-1' } });
    expect(screen.getByText('Forward Parameter Variation')).toBeInTheDocument();
  });
  it('rejects other artifact types and preserves raw decimal evidence separately from finite presentation', () => {
    expect(decodeVariationReport({type:'other'})).toBeNull();
    const decoded = decodeVariationReport(fixture.development)!;
    expect(decoded.center?.metrics.total_return).toBe(0.12);
    expect(decoded.raw).toEqual(fixture.development);
  });
});
