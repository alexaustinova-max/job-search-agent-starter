import test from 'node:test';
import assert from 'node:assert/strict';
import {verifyHhHistory} from '../browser/extension-form-review.mjs';

test('all history scopes, stable data and sequential next avoid invisible numbered controls',async()=>{
 let scope='tab_filter_all',page=1;const visited=[];
 const tab={goto:async()=>{},playwright:{domSnapshot:async()=>{},evaluate:async()=>({scope,page,signature:scope+':'+page,found:page===2?['123']:[],matches:[],pages:[1,3],has_next:true,rows:20,link_count:20,expected_count:60,login:false,protection:false}),
 locator:selector=>({waitFor:async()=>{},getAttribute:async()=>selector.includes(scope)?'true':'false',click:async()=>{
  if(selector.includes('tab_filter_')){scope=selector.match(/tab_filter_\w+/)[0];page=1;return;}
  if(selector==='[data-qa="number-pages-next"]')page++;else {assert.equal(selector,'[data-qa~="number-pages-3"]');page=3;}visited.push([scope,page]);
 }})}};
 const r=await verifyHhHistory(tab,['123']);assert.equal(r.checked,true);assert.deepEqual(r.found,['123']);
 assert.equal(r.scopes.length,3);assert.equal(visited.length,6);for(const s of r.scopes)assert.deepEqual(s.pages,[1,2,3]);
});

test('missing page control never certifies complete history',async()=>{
 const tab={goto:async()=>{},playwright:{domSnapshot:async()=>{},evaluate:async()=>({scope:'tab_filter_all',page:1,signature:'first',found:[],matches:[],pages:[1,2],rows:20,link_count:20,expected_count:40}),locator:s=>({waitFor:async()=>{},getAttribute:async()=> 'true',click:async()=>{throw Error('control unavailable');}})}};
 await assert.rejects(verifyHhHistory(tab,['123']),/control unavailable/);
});
