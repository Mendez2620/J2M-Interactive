"use strict";

const walletReviewForm = document.querySelector("[data-wallet-review]");
if (walletReviewForm) {
    const count = walletReviewForm.querySelector("[data-wallet-selection-count]");
    const updateWalletSelectionCount = () => {
        const selected = walletReviewForm.querySelectorAll('input[name="candidate"]:checked').length;
        count.textContent = `${selected} clientes seleccionados de 25.`;
    };
    walletReviewForm.addEventListener("change", updateWalletSelectionCount);
    window.addEventListener("pageshow", updateWalletSelectionCount);
    updateWalletSelectionCount();
}
