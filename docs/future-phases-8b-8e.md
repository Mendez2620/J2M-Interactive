# Hoja de ruta funcional y backlog técnico

## Secuencia funcional aprobada

- **8B.1 — UX responsive de Staff/Gerente y escáner:** navegación de escritorio y móvil, accesibilidad, PWA e interfaz del escáner sin cambiar autenticación ni lógica contable.
- **8B.2 — Acceso unificado:** acceso habitual por correo y contraseña para gerentes y cajeros, con selección posterior de cuenta cuando corresponda y compatibilidad temporal para cajeros sin correo.
- **8C — Referidos, recompensas y antifraude:** reglas posteriores a primera compra válida, controles operativos y revisión de múltiples tarjetas sin prometer identificación universal.
- **8D — Wallet:** personalización, actualización y validación real en dispositivos.
- **8E — Registro público, consentimiento y revisión legal:** mejoras de contenido y políticas sin reintroducir correo obligatorio.

## Implementación de 8B.2

El acceso unificado recibe correo y contraseña sin solicitar negocio ni rol antes de autenticar. La búsqueda de candidatos es acotada, usa un presupuesto uniforme de comprobaciones de contraseña y solo presenta negocios y roles autorizados después de validar las credenciales. La selección crea una sesión independiente y revocable, ligada a negocio, rol, canal y versión de autorización. La protección contra enumeración conserva errores públicos genéricos y el acceso anterior permanece disponible exclusivamente para cajeros históricos sin correo durante una transición controlada. La arquitectura, los límites y el procedimiento de transición se documentan en `docs/staff-unified-access.md`.

## Backlog técnico adicional, sin numeración de fase

- **Recuperación asistida auditada:** verificación presencial, permisos explícitos, motivo obligatorio, auditoría inmutable y revocación; nunca solo nombre, teléfono o fecha de nacimiento.
- **Retención y minimización histórica:** política legal, exportación/rectificación, respaldos probados y anonimización incremental de correos históricos y artefactos OTP.
- **Canales de comunicación opt-in:** canales independientes y consentimientos versionados sin correo obligatorio en el alta.
- **Operación y observabilidad:** retirada controlada del worker y tablas históricas cuando no existan trabajos pendientes, más validación física de Wallet y PostgreSQL de staging.

Cada implementación requiere rama y PR propios, pruebas focalizadas y revisión antes de cualquier despliegue.
