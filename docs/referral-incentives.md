# Incentivos de referidos — Fase 8C.2A

Esta fase incorpora configuración tenant-scoped y evaluación **DRY-RUN**. No concede puntos, sellos, cupones, dinero ni comisiones; tampoco modifica `Customer`, el ledger de lealtad, transacciones, Wallet u outboxes.

## Contrato operativo

- `ReferralIncentiveRule` describe una regla versionada. Solo admite `draft` y `disabled`; no existe estado ni endpoint de activación o pago.
- La única condición aceptada es `first_verified_purchase`, respaldada por una `ReferralEligibility` de 8C.1 en estado `verified`, con transacción y evidencia auditada.
- El programa y la unidad de recompensa deben coincidir exactamente con `Business.loyalty_type`. No hay conversiones monetarias ni equivalencias entre puntos y sellos.
- `ReferralIncentiveRuleAudit` conserva snapshots append-only e idempotencia por negocio y solicitud.
- `ReferralIncentiveRecord` reserva el contrato de propuestas/reversos futuros. Un reverso debe ser una fila nueva que referencia la propuesta original; nunca un UPDATE o DELETE. Sus filas son append-only y únicas por evento, regla, versión, beneficiario y estado. 8C.2A no expone una operación que las cree ni las convierta en obligaciones cobrables.
- El motor `preview_incentive()` solo lee y devuelve uno de: `eligible_for_proposal`, `pending_verification`, `not_eligible`, `revoked`, `rule_disabled` o `conflict_or_duplicate`.

El único beneficiario resoluble actualmente es `external_promoter`, usando la atribución existente. `staff` y `customer_ambassador` forman parte del contrato de datos, pero la evaluación responde `not_eligible`: no se infieren identidades ni se crean atribuciones sin consentimiento.

## Seguridad

Las mutaciones de reglas requieren gerente activo, canal web, permiso `manage_referral_incentives`, tenant coincidente, CSRF, versión esperada e `Idempotency-Key`. Las actualizaciones usan comparación y actualización condicional. Auditorías y registros históricos tienen FKs `RESTRICT`, eventos ORM y triggers PostgreSQL/SQLite que impiden UPDATE o DELETE. PostgreSQL habilita RLS sin políticas públicas en las tres tablas.

No se usa IP, dispositivo, nombre, teléfono o Wallet para afirmar identidad física. La verificación manual de 8C.1 continúa siendo una atestación humana y no sustituye un POS o banco.

## Matriz de alcance

| Función | 8C.2A |
| --- | --- |
| Crear/editar reglas en borrador o deshabilitadas | Operativa mediante API protegida |
| Vista previa de promotor externo | Operativa, solo lectura |
| Concesión/pago/abono | Deshabilitada; no existe endpoint |
| Propuestas persistentes y reversos | Esquema preparado, sin operación pública |
| Staff referido sin cuenta paralela | Pendiente 8C.2B |
| Cliente embajador y consentimiento | Pendiente 8C.2B |
| Pestaña Referidos, panel y formularios visuales | Pendiente 8C.3 |
| Wallet de promotores/staff o actualización de pases | Fuera de alcance |

## Integración futura en staging

Tras aprobación y merge manual a `main`, preparar un PR separado hacia `staging`. Antes de migrar: confirmar respaldo/política del propietario, `alembic_version=1b2c3d4e5f6a`, servicios detenidos o coordinados y recuentos de tablas sensibles. Ejecutar una sola vez `flask --app run.py db upgrade 2c3d4e5f6a7b` desde código fijado al SHA aprobado. Después verificar head, tablas, FKs, RLS, triggers y recuentos intactos. No colocar `db upgrade` en el arranque, healthcheck o petición web. No ejecutar downgrade si existe historial.
