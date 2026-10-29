# Incentivos de referidos — Fase 8C.2A

Esta fase incorpora configuración tenant-scoped y evaluación **DRY-RUN**. No concede puntos, sellos, cupones, dinero ni comisiones; tampoco modifica `Customer`, el ledger de lealtad, transacciones, Wallet u outboxes.

## Contrato operativo

- `ReferralIncentiveRule` describe una regla versionada. Solo admite `draft` y `disabled`; no existe estado ni endpoint de activación o pago.
- La única condición aceptada es `first_verified_purchase`, respaldada por una `ReferralEligibility` de 8C.1 en estado `verified`, con transacción y evidencia auditada.
- El programa y la unidad de recompensa deben coincidir exactamente con `Business.loyalty_type`. No hay conversiones monetarias ni equivalencias entre puntos y sellos.
- `ReferralIncentiveRuleAudit` conserva snapshots append-only e idempotencia por negocio y solicitud. Un replay devuelve el snapshot y la versión que produjo la solicitud original, además de `current_rule_version`; nunca presenta el estado actual como si fuera la respuesta histórica. Reutilizar la clave con otro contenido devuelve `409`.
- `ReferralIncentiveRecord` reserva el contrato de propuestas/reversos futuros. La identidad económica estable es negocio + transacción fuente + tipo de beneficiario, independientemente de la regla o su versión. También almacena elegibilidad y atribución, y un guard transaccional verifica que coincidan entre sí y, para promotores, con el beneficiario. Un reverso debe ser una fila nueva que referencia la propuesta original; nunca un UPDATE o DELETE. 8C.2A no expone una operación que las cree ni las convierta en obligaciones cobrables.
- El motor `preview_incentive()` solo lee y devuelve uno de: `eligible_for_proposal`, `pending_verification`, `not_eligible`, `revoked`, `rule_disabled` o `conflict_or_duplicate`.

El único beneficiario resoluble actualmente es `external_promoter`, usando la atribución existente. `staff` y `customer_ambassador` forman parte del contrato de reglas, pero la evaluación responde `not_eligible` y los guards impiden crear registros para esos tipos: no se infieren identidades ni se crean atribuciones sin consentimiento.

### Límites y actualizaciones parciales

`period_limit` y `period_unit` forman un par indivisible: una actualización debe enviar ambos, o ambos como `null` para retirar el límite. Esto evita combinar accidentalmente valores de versiones distintas.

`day` y `month` significan periodos de calendario del negocio, no ventanas móviles de 24 horas o 31 días. El modelo actual no almacena una zona horaria IANA del negocio. Por ello, cualquier regla que configure un límite temporal se evalúa de forma fail-closed como `not_eligible`; no se inventa UTC ni una zona local. Habilitar estos límites requiere una decisión del propietario y una fase posterior que añada una zona horaria explícita y defina el tratamiento de cambios DST. Las alternativas documentadas para esa decisión son calendario comercial local o ventanas móviles expresamente renombradas.

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

`EXPECTED_ALEMBIC_HEAD` cambia junto con el código, por lo que una instancia nueva no debe arrancar contra una base aún situada en `1b2c3d4e5f6a`. La secuencia coordinada es: construir y fijar el SHA aprobado; pausar web y workers o activar una ventana de mantenimiento; comprobar respaldo y head `1b2c3d4e5f6a`; ejecutar una única tarea de migración con la imagen nueva; verificar `2c3d4e5f6a7b`; y solo entonces sustituir web y workers. Si Railway no permite garantizar ese orden en un despliegue rodante, no se despliega hasta disponer de una ventana coordinada; no se relaja el preflight.
