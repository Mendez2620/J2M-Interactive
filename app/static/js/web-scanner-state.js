(function (global, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  global.J2MScannerState = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  class ScannerState {
    constructor() {
      this.phase = 'idle';
      this.lookupVersion = 0;
      this.selection = null;
      this.pendingOperation = null;
    }

    beginLookup() {
      if (this.phase === 'processing' || this.phase === 'uncertain') return null;
      this.lookupVersion += 1;
      this.selection = null;
      this.phase = 'lookup';
      return this.lookupVersion;
    }

    finishLookup(version, selection) {
      if (version !== this.lookupVersion || this.phase !== 'lookup') return false;
      this.selection = selection || null;
      this.phase = selection ? 'selected' : 'idle';
      return true;
    }

    beginOperation(operation) {
      if (this.phase === 'selected' && this.selection) {
        this.pendingOperation = operation;
        this.phase = 'processing';
        return operation;
      }
      if (this.phase === 'uncertain' && this.pendingOperation?.signature === operation.signature) {
        this.phase = 'processing';
        return this.pendingOperation;
      }
      return null;
    }

    finishOperation(outcome, customer) {
      if (this.phase !== 'processing' || !this.pendingOperation) return false;
      if (outcome === 'ambiguous') {
        this.phase = 'uncertain';
        return true;
      }
      if (outcome === 'success' && customer && this.selection) {
        this.selection.customer = {...this.selection.customer, ...customer};
      }
      this.pendingOperation = null;
      this.phase = this.selection ? 'selected' : 'idle';
      return true;
    }

    reset() {
      if (this.phase === 'processing' || this.phase === 'uncertain') return false;
      this.lookupVersion += 1;
      this.selection = null;
      this.pendingOperation = null;
      this.phase = 'idle';
      return true;
    }
  }

  return {ScannerState};
});
