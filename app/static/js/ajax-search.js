(() => {
  document.querySelectorAll('[data-copy-text]').forEach((button) => button.addEventListener('click', async () => {
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(button.dataset.copyText);
      button.textContent = 'Enlace copiado';
    } catch (_error) {
      const fallback = document.createElement('input');
      fallback.value = button.dataset.copyText;
      fallback.readOnly = true;
      fallback.className = 'field mt-2';
      fallback.setAttribute('aria-label', 'Enlace para copiar manualmente');
      button.insertAdjacentElement('afterend', fallback);
      fallback.select();
      button.textContent = 'Copia el enlace mostrado';
    }
    window.setTimeout(() => { button.textContent = original; }, 1800);
  }));
  document.querySelectorAll('form[data-ajax-search]').forEach((form) => {
    const selector = form.dataset.ajaxTarget;
    const target = document.querySelector(selector);
    if (!target) return;
    let timer;
    let controller;
    const load = async (url, push = true) => {
      if (controller) controller.abort();
      controller = new AbortController();
      target.setAttribute('aria-busy', 'true');
      target.classList.add('opacity-60');
      try {
        const response = await fetch(url, {headers: {'X-Requested-With': 'XMLHttpRequest'}, signal: controller.signal});
        if (!response.ok) throw new Error('request failed');
        const copy = new DOMParser().parseFromString(await response.text(), 'text/html');
        const replacement = copy.querySelector(selector);
        if (!replacement) throw new Error('target missing');
        target.replaceChildren(...Array.from(replacement.childNodes, (node) => document.importNode(node, true)));
        if (push) history.pushState({ajaxSearch: true}, '', url);
      } catch (error) {
        if (error.name !== 'AbortError') {
          const status = document.createElement('p');
          status.className = 'p-4 text-sm text-rose-700';
          status.setAttribute('role', 'alert');
          status.textContent = 'No fue posible actualizar los resultados. Intenta nuevamente.';
          target.prepend(status);
        }
      } finally {
        target.removeAttribute('aria-busy');
        target.classList.remove('opacity-60');
      }
    };
    const submit = () => load(`${form.action}?${new URLSearchParams(new FormData(form))}`);
    form.addEventListener('submit', (event) => { event.preventDefault(); submit(); });
    form.querySelectorAll('input[type="search"], input[name="q"]').forEach((input) => input.addEventListener('input', () => {
      clearTimeout(timer); timer = setTimeout(submit, 300);
    }));
    form.querySelectorAll('select, input[type="date"]').forEach((input) => input.addEventListener('change', submit));
    target.addEventListener('click', (event) => {
      const link = event.target.closest('a[href]');
      if (!link || new URL(link.href).origin !== location.origin) return;
      event.preventDefault(); load(link.href);
    });
    window.addEventListener('popstate', () => load(location.href, false));
  });
})();
