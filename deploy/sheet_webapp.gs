/**
 * 자산배분 성과 시트 — Pi(performance.py)가 보내는 성과 데이터를 받아 시트에 쓰는 웹 앱.
 * 설치: 시트 → 확장 프로그램 → Apps Script → 이 코드 붙여넣기 → 배포 → 새 배포 → 웹 앱
 *       (실행: 나, 액세스 권한: 모든 사용자) → 웹 앱 URL을 Pi .env SHEET_WEBAPP_URL에 넣기.
 * TOKEN은 Pi .env SHEET_TOKEN과 같아야 함(다른 사람이 가짜 데이터를 못 쓰게).
 */
const TOKEN = '여기에_Pi_.env의_SHEET_TOKEN_값';

function doPost(e) {
  const data = JSON.parse(e.postData.contents);
  if (data.token !== TOKEN) {
    return ContentService.createTextOutput('unauthorized');
  }
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  writeTable_(ss, '요약', data.summary, '#,##0');
  writeTable_(ss, '월별', data.monthly, '0.0%');
  writeTable_(ss, '기록', data.history, '#,##0');
  writeTable_(ss, '자금이동', data.flows, '#,##0');
  const s = ss.getSheetByName('요약');
  s.getRange(data.summary.length + 2, 1).setValue('마지막 갱신: ' + data.updated_at);
  formatSummary_(s, data.summary.length);
  formatHistory_(ss.getSheetByName('기록'));
  ensureChart_(ss);
  return ContentService.createTextOutput('ok');
}

function writeTable_(ss, name, rows, numFmt) {
  let sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  sh.clearContents();
  if (!rows || !rows.length) return;
  sh.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
  sh.getRange(1, 1, 1, rows[0].length).setFontWeight('bold').setBackground('#e8eaed');
  sh.setFrozenRows(1);
  if (rows.length > 1 && rows[0].length > 1) {
    sh.getRange(2, 2, rows.length - 1, rows[0].length - 1).setNumberFormat(numFmt);
  }
}

function formatSummary_(sh, n) {
  // 요약: 슬리브, 평가액, 이번달, 연초이후, 고점대비, 낙폭한도, 상태
  if (n < 2) return;
  sh.getRange(2, 3, n - 1, 4).setNumberFormat('0.0%');
  sh.autoResizeColumns(1, 7);
}

function formatHistory_(sh) {
  // 기록: 날짜, 평가액 4개, 누적지수 4개, 낙폭 4개
  const last = sh.getLastRow();
  if (last < 2) return;
  sh.getRange(2, 6, last - 1, 4).setNumberFormat('0.000');
  sh.getRange(2, 10, last - 1, 4).setNumberFormat('0.0%');
}

function ensureChart_(ss) {
  const sh = ss.getSheetByName('요약');
  if (sh.getCharts().length) return;
  const hist = ss.getSheetByName('기록');
  const chart = sh.newChart().asLineChart()
    .addRange(hist.getRange('A:A'))
    .addRange(hist.getRange('F:I'))
    .setPosition(10, 1, 0, 0)
    .setOption('title', '누적 수익 (시간가중, 시작 = 1.0)')
    .setOption('width', 720).setOption('height', 360)
    .build();
  sh.insertChart(chart);
}
