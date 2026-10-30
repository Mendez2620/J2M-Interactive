# Acceso unificado del personal

La ruta habitual `/staff-access` autentica gerentes y cajeros mediante correo y contraseña sin solicitar el negocio. La búsqueda usa un índice funcional sobre `lower(staff.email)`, limita los candidatos y mantiene un presupuesto fijo de verificaciones de contraseña por intento. Los errores previos a la autenticación son genéricos y el rate limit persistente reserva cuota por identidad normalizada e IP.

Si las credenciales autorizan varias cuentas, la sesión firmada conserva durante cinco minutos solamente ID de staff, negocio, rol y `auth_version`. La selección posterior exige CSRF y vuelve a comprobar negocio, rol, canal web, permisos, estado y versión antes de crear una `StaffSession` exclusiva para esa cuenta. No se guarda la contraseña.

Los cajeros históricos sin correo conservan temporalmente `/staff/login?legacy=1`, que requiere usuario, negocio y contraseña. El POST legado rechaza con el mismo error genérico a cualquier cuenta que ya tenga correo, incluso si se proporciona su usuario o correo. Un gerente con `edit_cashiers` puede asignar un correo validado desde Cajeros; desde ese momento la cuenta debe usar `/staff-access`. El período de transición se mantiene únicamente mientras existan cajeros activos sin correo y debe revisarse antes de cada despliegue mediante métricas agregadas, nunca publicando identidades. No se modifican clientes de lealtad ni se les exige correo.

Las rutas históricas `/manager/login` y `/staff/login` muestran el acceso unificado en GET. El POST histórico de gerente se conserva para compatibilidad controlada; el POST histórico de cajero queda restringido a cuentas sin correo. El acceso legacy debe retirarse en una fase posterior solamente después de medir que no queden cajeros activos sin correo.

El POST histórico de gerente reutiliza la misma búsqueda acotada y el mismo presupuesto fijo de hashes que `/staff-access`. Solo admite roles `manager`; una identidad por correo puede producir el selector únicamente después de validar la contraseña, mientras que `username + business_slug` queda ligado a ese tenant. Ambos endpoints reservan la cuota persistente `staff-access`, por lo que alternarlos no duplica el presupuesto por identidad o IP.

## Correos compartidos por más cuentas que el límite

Si más cuentas comparten un correo que `STAFF_ACCESS_MAX_EMAIL_CANDIDATES`, el acceso falla de forma cerrada con el mismo mensaje genérico y no crea selector ni sesión. No se amplía la consulta, no se prueban candidatos fuera del presupuesto y no se revelan negocios. La resolución actual es administrativa: un Super Admin o gerente autorizado debe identificar de forma privada las cuentas vigentes, asignar correos individuales a las cuentas activas y retirar o corregir correos de cuentas obsoletas. Después de dejar el grupo dentro del límite, el acceso vuelve a funcionar normalmente. Cambiar el límite requiere una revisión explícita de capacidad y temporización y nunca debe superar el tope interno acotado.

## Migración segura en staging

El índice de acceso unificado se introdujo en `0a1b2c3d4e5f`; el head vigente de la aplicación es `3d4e5f6a7b8c`. El índice es incremental y coincide con la consulta de autenticación `lower(staff.email) = <correo normalizado>`.

Orden controlado para staging, ejecutado por el propietario:

1. Detener el despliegue, comprobar que no haya otro migrador activo y crear un respaldo verificable de PostgreSQL. Probar su restauración en un entorno aislado.
2. Confirmar la revisión previa con `SELECT version_num FROM alembic_version;`; debe ser exactamente `f9a0b1c2d3e4`.
3. Ejecutar una sola vez `flask --app run.py db upgrade 0a1b2c3d4e5f` desde una tarea controlada con la misma versión de código del PR aprobado.
4. Confirmar `0a1b2c3d4e5f` en `alembic_version` y comprobar el índice con `SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'staff' AND indexname = 'ix_staff_email_lower';`. La definición debe contener `lower((email)::text)` o la representación equivalente de PostgreSQL.
5. Ejecutar el preflight y pruebas de acceso con una cuenta de gerente, una de cajero con correo y, si existe, una cuenta histórica sin correo. Solo entonces desplegar web y workers coordinadamente.

Si la migración falla, no desplegar la aplicación ni reintentar en bucle. Conservar logs sanitizados y el respaldo. Si Alembic no avanzó, corregir la causa en un clon aislado. Si avanzó y únicamente debe retirarse este índice, ejecutar de forma controlada `flask --app run.py db downgrade f9a0b1c2d3e4` y verificar revisión y ausencia del índice; ante cualquier estado incierto, restaurar el respaldo en un entorno aislado antes de decidir la recuperación. Nunca ejecutar estos pasos automáticamente contra Supabase real.
