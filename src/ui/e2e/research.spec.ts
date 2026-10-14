import { test, expect } from '@playwright/test';

test('SDK exhibits use production tabs, provenance, source inspection and registered visual grammars',async({page})=>{
 await page.goto('/demo/research');const card=page.getByTestId('study-tool-card');const tabs=card.getByRole('tablist',{name:'Research views'});
 await expect(tabs).toBeVisible();await expect(card.getByTestId('harness-execution-card')).toHaveCount(0);
 await card.getByTestId('research-plot').getByRole('button').first().click();
 await expect(card.getByTestId('selected-evidence')).toContainText('observation_id');
 await card.getByRole('button',{name:'Inspect source row',exact:false}).click();
 await expect(card.getByText('1–1 of 1 rows',{exact:true})).toBeVisible();
 await expect(card.getByText('Tool evidence',{exact:true})).toBeVisible();
 await card.getByRole('combobox',{name:'Data',exact:true}).selectOption(JSON.stringify(['wavelets','research_view']));
 await expect(card.getByRole('tabpanel').locator('pre').first()).toContainText('panels');
 await tabs.getByRole('tab',{name:'Time × scale energy',exact:true}).click();
 await tabs.getByRole('tab',{name:'Time × scale energy',exact:true}).press('End');
 await expect(tabs.getByRole('tab',{name:'Returned data',exact:true})).toBeFocused();
 await tabs.getByRole('tab',{name:'Returned data',exact:true}).press('Home');
 await expect(tabs.getByRole('tab',{name:'Time × scale energy',exact:true})).toBeFocused();
 await card.getByRole('button',{name:'Expand',exact:true}).click();
 await expect(card.getByRole('button',{name:'Close expanded view',exact:true})).toBeVisible();
 await card.getByRole('button',{name:'Close expanded view',exact:true}).click();
 for(const [family,title] of [['seasonality','Session clock'],['events','Confirmed events'],['regimes','Published transitions'],['predictive signals','Published horizon profile'],['liquidity','Published cost distribution'],['parameters','Joint experiment landscape'],['falsification','Published null distribution'],['learned models','Training and evaluation windows']]){
  await page.getByRole('navigation',{name:'Research families'}).getByRole('button',{name:family,exact:true}).click();
  await tabs.getByRole('tab',{name:title,exact:true}).click();
  await expect(card.getByRole('heading',{name:title,exact:true})).toBeVisible();
  await expect(card.getByTestId('research-plot')).toBeVisible();
  await expect(card.getByRole('alert')).toHaveCount(0);
 }
});
