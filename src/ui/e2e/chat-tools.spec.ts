import { test, expect } from '@playwright/test';

test('activity rail keeps routine receipts compact and sketch details inspectable',async({page})=>{
 await page.setViewportSize({width:1440,height:1100});
 await page.goto('/demo/chat-tools');
 const receipts=page.getByTestId('tool-result-card');
 const reference=receipts.filter({has:page.locator('[data-activity-icon=book]')}).first();
 await expect(reference.getByTestId('tool-result-status')).toHaveText('completed');
 await expect(reference.getByTestId('tool-metadata')).not.toBeVisible();
 await reference.locator('summary').first().click();
 await expect(reference.getByTestId('tool-metadata')).toBeVisible();
 await expect(reference).toContainText('Publish observations');
 await reference.locator('summary').first().click();
 const sketch=page.getByTestId('sketch-tool-card');
 await expect(page.locator('[data-tool=read_strategy_sketch]')).toHaveClass(/prominent/);
 await expect(sketch.getByRole('heading',{name:'Intraday scale reversion'})).toBeVisible();
 const bounds=await sketch.boundingBox();
 expect(bounds!.height).toBeLessThan(450);
 expect(bounds!.width).toBeLessThan(720);
 await expect(sketch.getByText('The displacement is temporary.',{exact:true})).not.toBeVisible();
 await sketch.getByText('Assumptions · 3',{exact:false}).click();
 await expect(sketch.getByText('The displacement is temporary.',{exact:true})).toBeVisible();
 await sketch.getByText('Assumptions · 3',{exact:false}).click();
 await sketch.getByText('Mechanism & trading rules',{exact:true}).click();
 await expect(sketch.getByRole('definition').filter({hasText:'Short-lived liquidity shocks'})).toBeVisible();
 await sketch.getByText('Mechanism & trading rules',{exact:true}).click();
 await sketch.getByText('Changed in this revision · 1 section',{exact:true}).click();
 await expect(sketch.getByText(/Exit after a fixed holding horizon/)).toBeVisible();
 await sketch.getByText('Changed in this revision · 1 section',{exact:true}).click();
 await sketch.getByText('Exact revision and source',{exact:true}).click();
 await expect(sketch.locator('pre').last()).toContainText('demo-revision-b');
 await sketch.getByText('Exact revision and source',{exact:true}).click();
 await expect(page.getByTestId('tool-failure')).toBeVisible();
 await page.screenshot({path:'../../.local/chat-activity-rail.png',fullPage:true});
 await page.setViewportSize({width:390,height:1000});
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await sketch.getByText('Mechanism & trading rules',{exact:true}).click();
 await expect(sketch.getByRole('definition').filter({hasText:'Short-lived liquidity shocks'})).toBeVisible();
});

test('rail joins adjacent tools across collapsed and expanded receipts in root and scoped chats',async({page})=>{
 await page.goto('/demo/chat-tools');
 for(const surface of ['Scoped chat','Root chat']){
  await page.getByRole('button',{name:surface,exact:true}).click();
  const cards=page.getByTestId('tool-result-card');
  await expect(cards).toHaveCount(10);
  async function expectJoined(previous:number,next:number){
   const a=await cards.nth(previous).boundingBox();
   const bridge=await cards.nth(next).evaluate(el=>{const box=el.getBoundingClientRect();const style=getComputedStyle(el,'::before');return {start:box.top+parseFloat(style.top),end:box.top+parseFloat(style.top)+parseFloat(style.height),content:style.content};});
   expect(bridge.content).not.toBe('none');
   expect(bridge.start).toBeLessThanOrEqual(a!.y+a!.height+1);
   expect(bridge.end).toBeGreaterThanOrEqual((await cards.nth(next).boundingBox())!.y-1);
  }
  await expectJoined(0,1);
  await cards.nth(0).locator('summary').first().click();
  await expect(cards.nth(0).getByTestId('tool-metadata')).toBeVisible();
  await expectJoined(0,1);
  await expect(page.getByTestId('tool-failure')).toBeVisible();
  await expectJoined(3,4);
  expect(await cards.nth(3).evaluate(el=>getComputedStyle(el,'::before').content)).toBe('none');
 }
});

 test('SDK and newly registered reads expand without losing returned evidence',async({page})=>{
 await page.goto('/demo/chat-tools');
 for(const surface of ['Scoped chat','Root chat']){
  await page.getByRole('button',{name:surface,exact:true}).click();
  const sdk=page.getByTestId('tool-result-card').filter({has:page.getByText('Read SDK: consumer',{exact:true})});
  await expect(sdk.locator('pre').first()).not.toBeVisible();
  await sdk.locator('summary').first().focus();
  await page.keyboard.press('Enter');
  await expect(sdk.locator('pre').first()).toContainText('pub trait MarketConsumer');
  await expect(sdk.getByText(/Bounded excerpt/)).toBeVisible();
  await sdk.getByText('Returned payload',{exact:true}).click();
  await expect(sdk.getByTestId('reference-tool-card').locator('pre').last()).toContainText('demo-sdk');
  const other=page.getByTestId('tool-result-card').filter({has:page.getByText('Read an additional data source',{exact:true})});
  await expect(other.getByText('Full returned content from a newly registered read tool.',{exact:true})).not.toBeVisible();
  await other.locator('summary').first().click();
  await expect(other.getByText('Full returned content from a newly registered read tool.',{exact:true})).toBeVisible();
  await other.locator('summary').first().click();
  await other.locator('summary').first().click();
  await expect(other.getByText('Full returned content from a newly registered read tool.',{exact:true})).toBeVisible();
 }
});

 test('activity headers distinguish reads, search matches and returned list pages',async({page})=>{
 await page.goto('/demo/chat-tools');
 for(const surface of ['Scoped chat','Root chat']){
  await page.getByRole('button',{name:surface,exact:true}).click();
  const reference=page.locator('[data-tool=read_knowledge]').first();
  await expect(reference.locator('summary').first()).toContainText('Read: Wavelet research reference');
  await expect(reference.locator('[data-activity-icon=book]')).toBeVisible();
  await expect(reference.getByTestId('tool-result-count')).toHaveText('1 source');
  const failure=page.locator('[data-tool=read_knowledge]').last();
  await expect(failure.getByTestId('tool-result-count')).toHaveCount(0);
  await expect(failure.getByTestId('tool-failure')).toBeVisible();
  await expect(page.locator('[data-tool=validate_study] [data-activity-icon=dot]')).toBeVisible();
  const search=page.locator('[data-tool=search_knowledge]');
  await expect(search.locator('summary').first()).toContainText('Searched: intraday seasonality');
  await expect(search.locator('[data-activity-icon=search]')).toBeVisible();
  await expect(search.getByTestId('tool-result-count')).toHaveText('2 matches returned');
  const artifacts=page.locator('[data-tool=list_artifacts]');
  await expect(artifacts.locator('[data-activity-icon=list]')).toBeVisible();
  await expect(artifacts.getByTestId('tool-result-count')).toHaveText('3 returned');
  await artifacts.locator('summary').first().click();
  await expect(artifacts.getByText('observations.parquet',{exact:true})).toBeVisible();
  const list=page.locator('[data-tool=list_strategy_sketches]');
  await expect(list.locator('summary').first()).toContainText('Listed strategy sketches');
  await expect(list.getByTestId('tool-result-count')).toHaveText('2 returned');
  await list.locator('summary').first().click();
  await list.getByText('structured',{exact:true}).click();
  await expect(list.locator('pre').first()).toContainText('27');
  await expect(page.locator('[data-tool=list_studies]').getByTestId('tool-result-count')).toHaveText('0 returned');
  await expect(page.locator('[data-tool=inspect_new_source]').getByTestId('tool-result-count')).toHaveCount(0);
 }
 await page.setViewportSize({width:390,height:1000});
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.screenshot({path:'../../.local/chat-activity-headers-mobile.png',fullPage:true});
});
