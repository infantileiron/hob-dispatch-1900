/** @type {{token: string|null, account: import('../types/records.js').Account|null, stations: import('../types/records.js').Station[], shifts: import('../types/records.js').Shift[], rules: object[], mailbox: string, shiftId: number|null, queue: object|null, inbox: object[], importDraft: string, importKey: string|null, importPreview: object|null, importBatch: object|null, importBatches: object[]}} */
export const state = {token: sessionStorage.getItem('hob-token'), account: null, stations: [], shifts: [], rules: [], mailbox: '', shiftId: null, queue: null, inbox: [], importDraft: '', importKey: null, importPreview: null, importBatch: null, importBatches: []};

export function resetImport() {
  state.importDraft = '';
  state.importKey = null;
  state.importPreview = null;
  state.importBatch = null;
  state.importBatches = [];
}

export function setSession(session) {
  state.token = session?.token ?? null;
  state.account = session?.account ?? null;
  if (state.token) sessionStorage.setItem('hob-token', state.token);
  else sessionStorage.removeItem('hob-token');
  state.mailbox = '';
  state.shiftId = null;
  resetImport();
}
