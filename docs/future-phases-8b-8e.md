# Hoja de ruta funcional y backlog técnico

## Secuencia funcional aprobada

- **8B.1 — UX responsive de Staff/Gerente y escáner:** navegación de escritorio y móvil, accesibilidad, PWA e interfaz del escáner sin cambiar autenticación ni lógica contable.
- **8B.2 — Acceso unificado:** fase independiente para estudiar e implementar un acceso de gerente/cajero más sencillo.
- **8C — Referidos, recompensas y antifraude:** reglas posteriores a primera compra válida, controles operativos y revisión de múltiples tarjetas sin prometer identificación universal.
- **8D — Wallet:** personalización, actualización y validación real en dispositivos.
- **8E — Registro público, consentimiento y revisión legal:** mejoras de contenido y políticas sin reintroducir correo obligatorio.

## Propuesta técnica para 8B.2 — no implementada

El formulario podrá recibir correo y contraseña sin solicitar el slug. El backend buscará candidatos activos de forma no enumeradora, comprobará la contraseña con coste uniforme y, solo después de autenticar, presentará un selector de negocios y roles autorizados cuando exista ambigüedad. Cada selección creará una sesión independiente, revocable, ligada a negocio, rol, canal y versión de autorización. Los errores públicos seguirán siendo genéricos y el login actual permanecerá disponible durante una transición controlada. La protección contra enumeración deberá cubrir contenido, códigos HTTP, cookies y diferencias temporales previsibles. Esta propuesta requiere revisión específica de duplicados de correo, rate limits, CSRF, aislamiento tenant y revocación; no forma parte de 8B.1.

## Backlog técnico adicional, sin numeración de fase

- **Recuperación asistida auditada:** verificación presencial, permisos explícitos, motivo obligatorio, auditoría inmutable y revocación; nunca solo nombre, teléfono o fecha de nacimiento.
- **Retención y minimización histórica:** política legal, exportación/rectificación, respaldos probados y anonimización incremental de correos históricos y artefactos OTP.
- **Canales de comunicación opt-in:** canales independientes y consentimientos versionados sin correo obligatorio en el alta.
- **Operación y observabilidad:** retirada controlada del worker y tablas históricas cuando no existan trabajos pendientes, más validación física de Wallet y PostgreSQL de staging.

Cada implementación requiere rama y PR propios, pruebas focalizadas y revisión antes de cualquier despliegue.
