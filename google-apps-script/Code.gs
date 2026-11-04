// Fixed-recipient report delivery only. No GmailApp, DriveApp or external requests.
// Set this in YOUR deployment's Script Properties. Never supplied by the sender.
const REPORT_RECIPIENT = PropertiesService.getScriptProperties().getProperty('JOB_REPORT_RECIPIENT');

function jsonReply_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}

function tokenMatches_(received, expected) {
  if (typeof received !== 'string' || typeof expected !== 'string' ||
      !/^[a-f0-9]{64}$/.test(received) || !/^[a-f0-9]{64}$/.test(expected)) return false;
  let difference = 0;
  for (let i = 0; i < 64; i++) difference |= received.charCodeAt(i) ^ expected.charCodeAt(i);
  return difference === 0;
}

function hexDigest_(bytes) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes)
    .map(value => ('0' + ((value + 256) % 256).toString(16)).slice(-2)).join('');
}

// Run once in the editor to authorize only MailApp; this function sends no email.
function authorizeMailApp() {
  return MailApp.getRemainingDailyQuota();
}

function doGet() {
  return jsonReply_({service: 'job-search-agent-report', version: 1});
}

function doPost(e) {
  let request;
  try {
    if (!e || !e.postData || e.postData.contents.length > 8000000) return jsonReply_({status: 'invalid_request'});
    request = JSON.parse(e.postData.contents);
  } catch (_) { return jsonReply_({status: 'invalid_request'}); }
  if (typeof REPORT_RECIPIENT !== 'string' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(REPORT_RECIPIENT)) {
    return jsonReply_({status: 'recipient_not_configured'});
  }
  const properties = PropertiesService.getScriptProperties();
  if (!request || !tokenMatches_(request.token, properties.getProperty('JOB_REPORT_TOKEN'))) {
    return jsonReply_({status: 'unauthorized'});
  }
  const day = request.report_date;
  const test = request.report_id === 'test:' + day + ':mailapp-v1';
  const parsedDay = typeof day === 'string' ? new Date(day + 'T00:00:00Z') : new Date(NaN);
  if (typeof day !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(day) ||
      isNaN(parsedDay.getTime()) || parsedDay.toISOString().slice(0, 10) !== day ||
      (!test && request.report_id !== 'daily:' + day) ||
      typeof request.subject !== 'string' ||
      !new RegExp('^Job Search Agent · Отчет за ' + day.slice(8,10) + '\\.' + day.slice(5,7) + '\\.' + day.slice(0,4) + ' · \\d+ откликов$').test(request.subject) ||
      typeof request.text_body !== 'string' || typeof request.html_body !== 'string' ||
      typeof request.digest_html_base64 !== 'string' || typeof request.digest_sha256 !== 'string') {
    return jsonReply_({status: 'invalid_report'});
  }
  let attachment;
  try {
    const bytes = Utilities.base64Decode(request.digest_html_base64);
    if (bytes.length === 0 || bytes.length > 5000000 || hexDigest_(bytes) !== request.digest_sha256 ||
        Utilities.newBlob(request.text_body + request.html_body).getBytes().length > 180000) {
      return jsonReply_({status: 'invalid_report_size_or_checksum'});
    }
    attachment = Utilities.newBlob(bytes, 'text/html', 'digest.html');
  } catch (_) { return jsonReply_({status: 'invalid_attachment'}); }
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return jsonReply_({status: 'busy_no_send'});
  const key = (test ? 'JSA_TEST_' : 'JSA_REPORT_') + day;
  try {
    const previous = properties.getProperty(key);
    if (previous) return jsonReply_({status: 'already_recorded_no_resend', report_id: request.report_id});
    if (MailApp.getRemainingDailyQuota() < 1) return jsonReply_({status: 'mail_quota_exhausted_no_send'});
    // Reserve BEFORE calling MailApp. A crash/uncertain error must never resend.
    properties.setProperty(key, JSON.stringify({status: 'attempt_reserved', report_id: request.report_id, time: new Date().toISOString()}));
    try {
      MailApp.sendEmail({to: REPORT_RECIPIENT,
        subject: (test ? '[ТЕСТ mailapp-v1] ' : '') + request.subject,
        body: request.text_body, htmlBody: request.html_body,
        attachments: [attachment], name: 'Job Search Agent'});
      const sentAt = new Date().toISOString();
      properties.setProperty(key, JSON.stringify({status: 'mailapp_accepted', report_id: request.report_id, time: sentAt}));
      return jsonReply_({status: 'mailapp_accepted', report_id: request.report_id,
        recipient: REPORT_RECIPIENT, digest_sha256: request.digest_sha256, sent_at: sentAt});
    } catch (_) {
      properties.setProperty(key, JSON.stringify({status: 'send_uncertain_no_retry', report_id: request.report_id}));
      return jsonReply_({status: 'send_uncertain_no_retry', report_id: request.report_id});
    }
  } finally { lock.releaseLock(); }
}
