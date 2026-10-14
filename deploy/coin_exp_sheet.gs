/**
 * 코인 실험봇 성과 시트 — Pi(coin_exp.py push_sheet)가 보내는 실험 봇 성과를 받아 시트에 쓰는 웹 앱.
 * 설치: 새 스프레드시트 → 확장 프로그램 → Apps Script → 이 코드 붙여넣기 → 배포 → 새 배포 → 웹 앱
 *       (실행: 나, 액세스 권한: 모든 사용자) → 웹 앱 URL을 Pi .env COINEXP_SHEET_URL에 넣기.
 * TOKEN은 Pi .env SHEET_TOKEN과 같아야 함(자산배분 성과 시트와 같은 값).
 */
const TOKEN = '여기에_Pi_.env의_SHEET_TOKEN_값';

function doPost(e) {
  const data = JSON.parse(e.postData.contents);
  if (data.token !== TOKEN) {
    return ContentService.createTextOutput('unauthorized');
  }
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  writeTable_(ss, '요약', data.summary, '#,##0');
  writeTable_(ss, '추이', data.history, '#,##0');
  writeTable_(ss, '거래기록', data.trades, '#,##0.########');
  const s = ss.getSheetByName('요약');
  const n = data.summary.length;
  if (n > 1) {
    s.getRange(2, 3, n - 1, 3).setNumberFormat('0.0%');   // 실험 수익률, 그냥 보유, 차이
    s.getRange(2, 7, n - 1, 1).setNumberFormat('0.0');     // 평균 비용(bp)
  }
  s.getRange(n + 2, 1).setValue('마지막 갱신: ' + data.updated_at);
  s.getRange(n + 3, 1).setValue('평가 시점: 2027-01 분기 검토(최소 3개월). 실험 장부는 코인당 1만 원으로 시작.');
  s.autoResizeColumns(1, 8);
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

function ensureChart_(ss) {
  // 추이: 시각, 실험 합계, 보유 합계, (코인별 실험·보유 ...) — 합계 두 줄만 그래프로
  const sh = ss.getSheetByName('요약');
  if (sh.getCharts().length) return;
  const hist = ss.getSheetByName('추이');
  const chart = sh.newChart().asLineChart()
    .addRange(hist.getRange('A:C'))
    .setPosition(12, 1, 0, 0)
    .setOption('title', '실험 봇 vs 그냥 보유 (3코인 합계, 원)')
    .setOption('width', 720).setOption('height', 360)
    .build();
  sh.insertChart(chart);
}
