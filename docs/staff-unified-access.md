# Acceso unificado del personal

La ruta habitual `/staff-access` autentica gerentes y cajeros mediante correo y contraseña sin solicitar el negocio. La búsqueda usa un índice funcional sobre `lower(staff.email)`, limita los candidatos y mantiene un presupuesto fijo de verificaciones de contraseña por intento. Los errores previos a la autenticación son genéricos y el rate limit persistente reserva cuota por identidad normalizada e IP.

Si las credenciales autorizan varias cuentas, la sesión firmada conserva durante cinco minutos solamente ID de staff, negocio, rol y `auth_version`. La selección posterior exige CSRF y vuelve a comprobar negocio, rol, canal web, permisos, estado y versión antes de crear una `StaffSession` exclusiva para esa cuenta. No se guarda la contraseña.

Los cajeros históricos con `email = NULL` conservan temporalmente `/staff/login?legacy=1`, que requiere usuario, negocio y contraseña. Un gerente con `edit_cashiers` puede asignar un correo validado desde Cajeros. No se modifican clientes de lealtad ni se les exige correo.

Las rutas históricas `/manager/login` y `/staff/login` muestran el acceso unificado en GET; sus POST anteriores se conservan para compatibilidad controlada. El acceso legacy debe retirarse en una fase posterior solamente después de medir que no queden cajeros activos sin correo.
