(() => {
  const input = document.getElementById('referralUrl');
  const status = document.querySelector('[data-share-status]');
  if (!input || !status) return;
  const copy = async () => {
    await navigator.clipboard.writeText(input.value);
    status.textContent = 'Enlace copiado.';
  };
  document.querySelector('[data-copy-referral]')?.addEventListener('click', () => copy().catch(() => {
    input.select();
    status.textContent = 'Selecciona y copia el enlace.';
  }));
  document.querySelector('[data-share-referral]')?.addEventListener('click', async () => {
    if (navigator.share) {
      try { await navigator.share({ title: 'Invitación', text: 'Únete al programa de lealtad.', url: input.value }); return; }
      catch (error) { if (error?.name === 'AbortError') return; }
    }
    try { await copy(); } catch (_) { input.select(); status.textContent = 'Selecciona y copia el enlace.'; }
  });
})();
