// Whether /appraise saved the appraisal. Without an estimate_id there is no
// saved estimate to attach a final price to, and the item won't be in history,
// so the page says so instead of offering a field that can't work.
// Kept in its own module so tests/frontend/save-status.test.mjs can run it.
export function saveStatus(data) {
  const isSaved = Number.isInteger(data?.estimate_id);
  return { isSaved, badge: isSaved ? null : 'Not saved' };
}
