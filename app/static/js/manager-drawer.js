(function (global, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  global.J2MManagerDrawer = api;
  if (global.document) api.initialize(global.document, global);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const FOCUSABLE_SELECTOR = [
    'a[href]',
    'button:not([disabled])',
    'input:not([type="hidden"]):not([disabled])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
  ].join(',');

  function focusableControls(container) {
    return [...container.querySelectorAll(FOCUSABLE_SELECTOR)].filter(element => {
      if (element.hidden || element.getAttribute('aria-hidden') === 'true') return false;
      if (typeof element.getClientRects === 'function' && element.getClientRects().length === 0) return false;
      return true;
    });
  }

  function initialize(document, view) {
    const drawer = document.getElementById('managerDrawer');
    const backdrop = document.querySelector('[data-manager-backdrop]');
    const openButton = document.querySelector('[data-open-manager-drawer]');
    const closeButton = document.querySelector('[data-close-manager-drawer]');
    if (!drawer || !backdrop || !openButton || drawer.dataset.drawerReady === 'true') return null;
    drawer.dataset.drawerReady = 'true';
    drawer.inert = true;
    let previousFocus = null;

    const close = ({restoreFocus = true} = {}) => {
      if (restoreFocus && previousFocus?.isConnected !== false) previousFocus?.focus?.();
      else if (drawer.contains(document.activeElement)) document.activeElement?.blur?.();
      drawer.classList.remove('is-open');
      backdrop.classList.remove('is-open');
      drawer.setAttribute('aria-hidden', 'true');
      drawer.inert = true;
      openButton.setAttribute('aria-expanded', 'false');
      document.body.classList.remove('overflow-hidden');
    };
    const open = () => {
      previousFocus = document.activeElement;
      drawer.inert = false;
      drawer.setAttribute('aria-hidden', 'false');
      drawer.classList.add('is-open');
      backdrop.classList.add('is-open');
      openButton.setAttribute('aria-expanded', 'true');
      document.body.classList.add('overflow-hidden');
      (closeButton || focusableControls(drawer)[0])?.focus?.();
    };
    const keydown = event => {
      if (!drawer.classList.contains('is-open')) return;
      if (event.key === 'Escape') { event.preventDefault(); close(); return; }
      if (event.key !== 'Tab') return;
      const focusable = focusableControls(drawer);
      if (!focusable.length) { event.preventDefault(); return; }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };

    openButton.addEventListener('click', open);
    closeButton?.addEventListener('click', () => close());
    backdrop.addEventListener('click', () => close());
    drawer.querySelectorAll('a[href]').forEach(link => link.addEventListener('click', () => close()));
    document.addEventListener('keydown', keydown);
    const desktop = view.matchMedia?.('(min-width: 1024px)');
    desktop?.addEventListener?.('change', event => { if (event.matches) close({restoreFocus:false}); });
    return {open, close, focusableControls: () => focusableControls(drawer)};
  }

  return {FOCUSABLE_SELECTOR, focusableControls, initialize};
});
