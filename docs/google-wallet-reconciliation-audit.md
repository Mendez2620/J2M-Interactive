# Fase 8E.2B.1 — Diagnóstico local de reconciliación Google Wallet

## Motivo y alcance

Cuando `GOOGLE_WALLET_MODE=disabled`, la transacción de lealtad sigue guardando el movimiento y el ledger, pero no crea un trabajo Google Wallet. Un trabajo completado de una transacción anterior no prueba que se haya sincronizado el último movimiento. El diagnóstico no reejecuta movimientos, no cambia saldos, no crea trabajos ni se conecta al proveedor.

El comando es de **solo lectura**, requiere un negocio explícito y funciona sin activar Google Wallet. Ejemplo exclusivamente en una base de ensayo autorizada:

```sh
flask --app run.py audit-google-wallet-reconciliation --business-id 1
```

No ejecutar contra la base operativa sin autorización del propietario. La salida JSON contiene únicamente agregados, sin nombres, teléfonos, tokens, identificadores de clientes ni saldos.

## Interpretación

Se clasifica la transacción registrada con mayor ID por cliente dentro del negocio (orden local de registro, no hora del proveedor). Se excluyen clientes sin transacciones. La consulta exige que los vínculos de negocio y cliente coincidan entre movimiento, ledger y trabajo antes de clasificarlo.

| Estado | Significado local |
| --- | --- |
| missing_job | Última transacción con entrada de ledger y sin trabajo propio. Requiere investigar antes de reconciliar. |
| pending / processing | Trabajo existente, no duplicar. |
| failed | Trabajo fallido, investigar y diseñar recuperación controlada. |
| completed | Trabajo local completado; no demuestra que el saldo remoto siga vigente. |
| missing_ledger | No existe entrada de ledger para el movimiento; no tratar como candidato válido. |
| invalid_linkage | Inconsistencia de negocio, cliente o tipo de movimiento. |
| unknown_job_status | Estado de trabajo no reconocido; no tratar como candidato válido. |

El resultado indica también si el negocio está activo. Esta clasificación es una fotografía local de cobertura, no certifica la cadena contable completa, la existencia de un pase remoto, el saldo actual de Google ni el motivo histórico por el cual falta un trabajo. Un trabajo anterior pendiente puede llegar a enviar el saldo actual. No inferir que todos los clientes con huecos poseen tarjetas.

## Próximo paso

Antes de introducir una recuperación que escriba en la cola, definir comprobación de objetos existentes, aislamiento por negocio, límites, idempotencia, concurrencia con el worker y tratamiento explícito de objetos ausentes. No emitir automáticamente tarjetas de clientes que nunca las solicitaron ni reconstruir el ledger. La activación real exige un ensayo autorizado, respaldo restaurable y revisión separada.
