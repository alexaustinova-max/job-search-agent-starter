import test from 'node:test';
import assert from 'node:assert/strict';
import {classifyForm,readFormDOM} from '../browser/extension-form-review.mjs';
const good=()=>({expected_url:true,form:true,cover_letter:true,submit_control:true,resume_control:true,
  coverage_complete:true,busy:false,questions:[],question_controls:[],closed_sections:[],notice:{questions:false},ready:'interactive'});
test('only an active in-form HH visibility warning blocks; body text and collapsed warning do not',()=>{
  const previous={document:globalThis.document,location:globalThis.location,getComputedStyle:globalThis.getComputedStyle};
  const warningText='Чтобы откликнуться, поменяйте видимость резюме';
  const element=(qa,{text='',height=30,overflow='visible',parent=null,tag='DIV'}={})=>({tagName:tag,id:'',textContent:text,innerText:text,parentElement:parent,
    styleFixture:{visibility:'visible',display:'block',opacity:'1',overflow,maxHeight:height===0?'0px':'none'},
    getAttribute:a=>a==='data-qa'?qa:null,getClientRects:()=>[{}],getBoundingClientRect:()=>({width:700,height}),querySelector:()=>null,querySelectorAll:()=>[],closest:()=>null});
  try{
    globalThis.location={href:'https://hh.ru/applicant/vacancy_response?vacancyId=123'};
    globalThis.getComputedStyle=e=>e.styleFixture;
    for(const [kind,expected] of [['body_only',false],['collapsed',false],['hidden_parent',false],['active',true]]){
      const form=element(null,{tag:'FORM'});form.id='RESPONSE_MODAL_FORM_ID';form.elements=[];form.contains=()=>true;
      const parent=kind==='hidden_parent'?element(null,{height:0,overflow:'hidden',parent:form}):form;
      const warning=element('hidden-resume-warning',{text:warningText,height:kind==='collapsed'?0:55,overflow:'hidden',parent});
      form.querySelectorAll=selector=>selector==='[data-qa="hidden-resume-warning"]'&&kind!=='body_only'?[warning]:[];
      globalThis.document={body:{innerText:warningText},readyState:'interactive',getElementById:id=>id===form.id?form:null,querySelector:()=>null,querySelectorAll:()=>[]};
      const read=readFormDOM('123');assert.equal(read.visibility_change_required,expected,kind);
      const classified=classifyForm({...good(),visibility_change_required:read.visibility_change_required},true);
      assert.equal(classified.reasons.includes('resume_visibility_requires_user'),expected,kind);
    }
  }finally{Object.assign(globalThis,previous);}
});
test('observed empty HH questionnaire view plus full stable DOM disambiguates generic header',()=>{
  const sample={...good(),notice:{questions:true,visible:true},question_view:{state:'absent_confirmed',factory_shape_verified:true,source:'active_HH_questionnaire_renderer'}};
  assert.equal(classifyForm(sample,true).questions_state,'absence_confirmed');
  for(const delta of [{coverage_complete:false},{busy:true},{question_view:{state:'unknown'}},
                     {question_view:{state:'present'}}]) {
    assert.equal(classifyForm({...sample,...delta},true).status,'needs_review');
  }
});

test('observed skip-question submit action is not an expander; real question controls remain blocking',()=>{
  const previous={document:globalThis.document,location:globalThis.location,getComputedStyle:globalThis.getComputedStyle};
  let qa='vacancy-response-link-no-questions';
  const button={tagName:'BUTTON',textContent:'Откликнуться без ответов на вопросы',getClientRects:()=>[{}],
    getAttribute:a=>a==='data-qa'?qa:null};
  const form={id:'RESPONSE_MODAL_FORM_ID',elements:[],getClientRects:()=>[{}],getAttribute:()=>null,
    querySelector:()=>null,contains:e=>e===button,querySelectorAll:()=>[]};
  try {
    globalThis.location={href:'https://hh.ru/applicant/vacancy_response?vacancyId=123'};
    globalThis.getComputedStyle=()=>({visibility:'visible'});
    globalThis.document={body:{innerText:''},readyState:'interactive',getElementById:id=>id===form.id?form:null,
      querySelector:()=>null,querySelectorAll:s=>s.startsWith('button,')?[button]:[]};
    assert.deepEqual(readFormDOM('123').question_controls,[]);
    qa='expand-questions';
    const controls=readFormDOM('123').question_controls;
    assert.equal(controls.length,1);
    assert.ok(classifyForm({...good(),question_controls:controls},true).reasons.includes('question_controls_not_resolved'));
  } finally {Object.assign(globalThis,previous);}
});

test('resume anchor uses the supplied owner title without a fixed profession',()=>{
  const previous={document:globalThis.document,location:globalThis.location,getComputedStyle:globalThis.getComputedStyle};
  const button={tagName:'BUTTON',textContent:'Демонстрационный координатор',getClientRects:()=>[{}],getAttribute:()=>null};
  const form={id:'RESPONSE_MODAL_FORM_ID',elements:[],getClientRects:()=>[{}],getAttribute:()=>null,querySelector:()=>null,contains:()=>true,
    querySelectorAll:s=>s==='button,[role="button"]'?[button]:[]};
  try {
    globalThis.location={href:'https://hh.ru/applicant/vacancy_response?vacancyId=900000001'};
    globalThis.getComputedStyle=()=>({visibility:'visible'});
    globalThis.document={body:{innerText:''},readyState:'interactive',getElementById:id=>id===form.id?form:null,querySelector:()=>null,querySelectorAll:s=>s.startsWith('button,')?[button]:[]};
    assert.equal(readFormDOM({vacancyId:'900000001',resumeTitles:['Демонстрационный координатор']}).resume_control,true);
    assert.equal(readFormDOM({vacancyId:'900000001',resumeTitles:['Другой профиль']}).resume_control,false);
    assert.equal(readFormDOM('900000001').resume_control,false);
  }finally{Object.assign(globalThis,previous);}
});
