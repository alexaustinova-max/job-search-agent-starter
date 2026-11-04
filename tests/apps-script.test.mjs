// Local Apps Script runtime doubles: no browser, Google calls or actual email.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import crypto from 'node:crypto';

const token = 'a'.repeat(64);
const source = fs.readFileSync(new URL('../google-apps-script/Code.gs', import.meta.url), 'utf8');
const manifest = JSON.parse(fs.readFileSync(new URL('../google-apps-script/appsscript.json', import.meta.url), 'utf8'));
assert.deepEqual(manifest.oauthScopes, ['https://www.googleapis.com/auth/script.send_mail']);
const attachment = Buffer.from('<html>Сохраненный отчет</html>');
const properties = new Map([['JOB_REPORT_TOKEN', token], ['JOB_REPORT_RECIPIENT','receiver@example.invalid']]);
const sent = []; let quota = 100; let failSend = false; let sendCalls = 0; let locked = false;
const context = vm.createContext({
  ContentService: {MimeType: {JSON: 'json'}, createTextOutput: value => ({value, setMimeType() { return this; }})},
  PropertiesService: {getScriptProperties: () => ({getProperty: key => properties.get(key) ?? null, setProperty: (key, value) => properties.set(key, value)})},
  LockService: {getScriptLock: () => ({tryLock: () => { assert.equal(locked, false); locked = true; return true; }, releaseLock: () => { locked = false; }})},
  Utilities: {DigestAlgorithm: {SHA_256: 'sha256'},
    computeDigest: (_, bytes) => [...crypto.createHash('sha256').update(Buffer.from(bytes)).digest()],
    base64Decode: text => [...Buffer.from(text, 'base64')],
    newBlob: (bytes, type, name) => ({bytes: Buffer.isBuffer(bytes) ? bytes : typeof bytes === 'string' ? Buffer.from(bytes) : Buffer.from(bytes), type, name,
      getBytes() { return [...this.bytes]; }})},
  MailApp: {getRemainingDailyQuota: () => quota, sendEmail: mail => {
    sendCalls++; if (failSend) throw Error('synthetic uncertainty'); sent.push(mail);
  }}
});
vm.runInContext(source, context);
const make = (day = '2026-11-04', test = true) => ({token, report_date: day,
  report_id: test ? `test:${day}:mailapp-v1` : `daily:${day}`,
  subject: `Job Search Agent · Отчет за ${day.slice(8)}.${day.slice(5,7)}.${day.slice(0,4)} · 1 откликов`,
  text_body: 'Вымышленный сохраненный отчет', html_body: '<p>Отчет</p>',
  digest_html_base64: attachment.toString('base64'), digest_sha256: crypto.createHash('sha256').update(attachment).digest('hex')});
const post = request => JSON.parse(context.doPost({postData: {contents: JSON.stringify(request)}}).value);
assert.equal(context.authorizeMailApp(), 100); assert.equal(sendCalls, 0);
assert.equal(post({...make(), token: 'b'.repeat(64)}).status, 'unauthorized'); assert.equal(sendCalls, 0);
assert.equal(post({...make(), digest_sha256: 'invalid'}).status, 'invalid_report_size_or_checksum');
assert.equal(post({...make(), report_date: '2026-99-99'}).status, 'invalid_report');
properties.set('JSA_REPORT_2026-11-04', 'old uncertain daily attempt');
assert.equal(post(make()).status, 'mailapp_accepted');
assert.equal(sent.length, 1); assert.equal(sent[0].to, 'receiver@example.invalid');
assert.equal(sent[0].attachments[0].name, 'digest.html'); assert.deepEqual(sent[0].attachments[0].bytes, attachment);
assert.equal(post(make()).status, 'already_recorded_no_resend'); assert.equal(sendCalls, 1);
assert.equal(properties.get('JSA_REPORT_2026-11-04'), 'old uncertain daily attempt');
assert.equal(post(make('2026-11-04', false)).status, 'already_recorded_no_resend');
assert.equal(post(make('2026-11-05', false)).status, 'mailapp_accepted'); assert.equal(sendCalls, 2);
failSend = true;
assert.equal(post(make('2026-11-06', false)).status, 'send_uncertain_no_retry');
assert.equal(post(make('2026-11-06', false)).status, 'already_recorded_no_resend'); assert.equal(sendCalls, 3);
failSend = false; quota = 0;
assert.equal(post(make('2026-11-07', false)).status, 'mail_quota_exhausted_no_send'); assert.equal(sendCalls, 3);
assert.equal(locked, false);
console.log('PASS: token, fixed recipient, exact attachment, invalid input, daily/test dedup, uncertainty, quota, mail-only scope; actual emails 0');
