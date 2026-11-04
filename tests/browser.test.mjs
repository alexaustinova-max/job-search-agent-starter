import test from 'node:test';
import assert from 'node:assert/strict';
import {preparationReasons,prepareApprovedForm,reviewPreparedForm} from '../browser/extension-form-review.mjs';

test('missing explicit admission is rejected before touching a browser',async()=>{
  const browser=new Proxy({}, {get(){throw Error('browser_must_not_be_used');}});
  const result=await prepareApprovedForm(browser,{});
  assert.equal(result.form_filled,false);
  assert.equal(result.submission_allowed,false);
  assert.ok(result.reasons.includes('user_preparation_approval_missing'));
});
test('review without an approved current candidate never touches a browser',async()=>{
  const result=await reviewPreparedForm(null,{});
  assert.equal(result.form_read,false);
  assert.equal(result.submission_allowed,false);
});
test('letter digest and exact resume mapping are mandatory',()=>{
  const reasons=preparationReasons({backend:'existing_chrome_extension',submission_allowed:false,
    status:'review_authorized',fingerprint:'fictional',candidate:{letter:'fictional'},
    preparation_approval:{source:'user',action:'prepare_without_submission'}});
  assert.ok(reasons.includes('exact_letter_not_approved'));
  assert.ok(reasons.includes('exact_HH_resume_mapping_missing'));
});
