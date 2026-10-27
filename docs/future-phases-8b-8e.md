# Fases futuras 8B–8E

Este documento delimita trabajo futuro; la Fase 8A no implementa ninguno de estos puntos.

- **8B — Recuperación asistida auditada:** diseñar verificación presencial, permisos explícitos, motivo obligatorio, auditoría inmutable y revocación de autorizaciones. No se basará solo en nombre, teléfono o fecha de nacimiento.
- **8C — Retención y minimización histórica:** acordar política legal, exportación/rectificación, respaldos probados y anonimización incremental de correos históricos y artefactos OTP.
- **8D — Canales de comunicación opt-in:** definir canales independientes y consentimientos versionados sin reintroducir correo obligatorio en el alta.
- **8E — Operación y observabilidad:** retirar de forma controlada el worker y tablas históricas cuando no existan trabajos pendientes, y validar recuperación/Wallet en dispositivos y PostgreSQL de staging.

Cada fase requiere rama y PR propios, pruebas focalizadas y revisión antes de cualquier despliegue.
