(() => {
  let registration = null;
  let updateRequested = false;
  let scannerPhase = document.querySelector('[data-web-scanner]')?.dataset.scannerPhase || 'idle';
  let updateButton = null;
  let updateMessage = null;

  const scannerIsBusy = () => scannerPhase === 'processing' || scannerPhase === 'uncertain';
  const showUpdate = worker => {
    if (!worker || document.querySelector('[data-pwa-update]')) return;
    const notice = document.createElement('aside');
    notice.dataset.pwaUpdate = '';
    notice.className = 'fixed inset-x-4 bottom-4 z-50 mx-auto max-w-md rounded-2xl border border-sky-200 bg-white p-4 shadow-xl';
    notice.setAttribute('role', 'status');
    const title = document.createElement('p');
    title.className = 'font-semibold text-slate-900';
    title.textContent = 'Hay una actualización disponible';
    updateMessage = document.createElement('p');
    updateMessage.className = 'mt-1 text-sm text-slate-600';
    updateMessage.textContent = 'Actualiza cuando hayas terminado el movimiento actual.';
    updateButton = document.createElement('button');
    updateButton.type = 'button';
    updateButton.className = 'btn-primary mt-3 w-full';
    updateButton.textContent = 'Actualizar aplicación';
    updateButton.addEventListener('click', () => {
      if (scannerIsBusy()) {
        updateButton.disabled = true;
        updateMessage.textContent = 'Termina o resuelve el movimiento pendiente antes de actualizar.';
        return;
      }
      updateRequested = true;
      updateButton.disabled = true;
      updateMessage.textContent = 'Aplicando actualización…';
      worker.postMessage({type:'SKIP_WAITING'});
    });
    notice.append(title, updateMessage, updateButton);
    document.body.appendChild(notice);
    updateButton.disabled = scannerIsBusy();
  };

  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/service-worker.js', {scope:'/'}).then(value => {
      registration = value;
      if (registration.waiting) showUpdate(registration.waiting);
      registration.addEventListener('updatefound', () => {
        const worker = registration.installing;
        worker?.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller) showUpdate(worker);
        });
      });
    }).catch(() => {});
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (updateRequested) window.location.reload();
    });
    window.addEventListener('online', () => registration?.update());
  }
  window.addEventListener('j2m:scanner-phase', event => {
    scannerPhase = event.detail?.phase || 'idle';
    if (updateButton) {
      updateButton.disabled = scannerIsBusy();
      if (!scannerIsBusy() && updateMessage) updateMessage.textContent = 'La actualización está lista para aplicarse.';
    }
  });
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
