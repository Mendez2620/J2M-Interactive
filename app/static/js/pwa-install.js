(() => {
  if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/service-worker.js', {scope:'/'});
  const buttons = document.querySelectorAll('[data-install-pwa]');
  const instructions = document.querySelectorAll('[data-ios-install]');
  const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  let promptEvent = null;
  if (standalone) { buttons.forEach(button => button.hidden = true); instructions.forEach(item => item.hidden = true); return; }
  if (ios) instructions.forEach(item => item.hidden = false);
  window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); promptEvent = event; buttons.forEach(button => button.hidden = false); });
  buttons.forEach(button => button.addEventListener('click', async () => { if (!promptEvent) return; promptEvent.prompt(); await promptEvent.userChoice; promptEvent = null; button.hidden = true; }));
  window.addEventListener('appinstalled', () => { buttons.forEach(button => button.hidden = true); instructions.forEach(item => item.hidden = true); });
})();
