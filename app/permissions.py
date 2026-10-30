"""Central permission catalogue and safe profile expansion."""

PERMISSION_CATALOG = {
    "view_stats": {"label": "Estadísticas", "description": "Consultar estadísticas del negocio.", "channel": "web", "roles": {"manager"}},
    "view_transactions": {"label": "Transacciones", "description": "Consultar transacciones del negocio.", "channel": "web", "roles": {"manager"}},
    "view_customers": {"label": "Clientes", "description": "Consultar clientes del negocio.", "channel": "web", "roles": {"manager"}},
    "review_referrals": {"label": "Revisar referidos", "description": "Revisar manualmente la elegibilidad de referidos.", "channel": "web", "roles": {"manager"}},
    "manage_referral_incentives": {"label": "Configurar incentivos", "description": "Configurar reglas deshabilitadas y consultar vistas previas de incentivos.", "channel": "web", "roles": {"manager"}},
    "manage_referral_participants": {"label": "Gestionar referidores", "description": "Habilitar o revocar la participación de empleados en referidos.", "channel": "web", "roles": {"manager"}},
    "authorize_customer_identity": {"label": "Vincular identidad de clientes", "description": "Autorizar presencialmente el enrolamiento o recuperación de credenciales de clientes.", "channel": "web", "roles": {"manager", "cashier"}},
    "refer_customers": {"label": "Recomendar clientes", "description": "Generar invitaciones de referido para este negocio.", "channel": "web", "roles": {"manager", "cashier"}},
    "export_reports": {"label": "Exportar reportes", "description": "Descargar reportes PDF y Excel.", "channel": "web", "roles": {"manager"}},
    "view_cashiers": {"label": "Consultar cajeros", "description": "Consultar empleados cajeros.", "channel": "web", "roles": {"manager"}},
    "create_cashiers": {"label": "Crear cajeros", "description": "Crear cuentas de cajero.", "channel": "web", "roles": {"manager"}},
    "edit_cashiers": {"label": "Editar cajeros", "description": "Editar datos básicos de cajeros.", "channel": "web", "roles": {"manager"}},
    "toggle_cashiers": {"label": "Activar o suspender", "description": "Activar o desactivar cajeros.", "channel": "web", "roles": {"manager"}},
    "reset_cashier_password": {"label": "Restablecer contraseñas", "description": "Restablecer contraseñas de cajeros.", "channel": "web", "roles": {"manager"}},
    "scanner_operate_web": {"label": "Operar escáner web", "description": "Consultar QR y confirmar movimientos desde el portal web.", "channel": "web", "roles": {"manager", "cashier"}},
    "scanner_operate": {"label": "Operar escáner", "description": "Consultar QR y procesar puntos o sellos.", "channel": "mobile", "roles": {"manager", "cashier"}},
}

PROFILE_PERMISSIONS = {
    "basic": {"view_stats"},
    "operational": {
        "view_stats", "view_transactions", "view_customers", "export_reports",
        "view_cashiers", "create_cashiers", "edit_cashiers", "toggle_cashiers",
    },
}

# The old capability keeps its original cashier-management scope, but never
# grants password reset or mobile access implicitly.
LEGACY_PERMISSION_ALIASES = {
    "manage_cashiers": {"view_cashiers", "create_cashiers"},
}


def stored_permissions(staff):
    return {item for item in (staff.permissions or "").split(",") if item}


def effective_permissions(staff):
    values = stored_permissions(staff)
    effective = set(values)
    for legacy, replacements in LEGACY_PERMISSION_ALIASES.items():
        if legacy in values:
            effective.update(replacements)
    return effective


def has_permission(staff, permission):
    return permission in effective_permissions(staff)


def valid_permissions_for(role, channel=None):
    return {
        key for key, definition in PERMISSION_CATALOG.items()
        if role in definition["roles"] and (channel is None or definition["channel"] == channel)
    }


def validate_permissions(role, values):
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("Los permisos deben enviarse como una lista de identificadores.")
    if len(values) != len(set(values)):
        raise ValueError("No se permiten permisos duplicados.")
    unknown = set(values) - set(PERMISSION_CATALOG)
    if unknown:
        raise ValueError("Se recibieron permisos desconocidos.")
    incompatible = set(values) - valid_permissions_for(role)
    if incompatible:
        raise ValueError("Uno o más permisos no son compatibles con el rol o canal.")
    return set(values)


def permissions_for_profile(role, profile, custom=None):
    if profile == "custom":
        return validate_permissions(role, custom)
    if profile not in PROFILE_PERMISSIONS or role != "manager":
        raise ValueError("Perfil incompatible con el rol indicado.")
    return PROFILE_PERMISSIONS[profile] & valid_permissions_for(role)


def detect_profile(staff):
    permissions = stored_permissions(staff)
    for profile, values in PROFILE_PERMISSIONS.items():
        if permissions == values:
            return profile
    return "custom"


def public_catalog(role):
    return {
        channel: [
            {"key": key, "label": item["label"], "description": item["description"]}
            for key, item in PERMISSION_CATALOG.items()
            if item["channel"] == channel and role in item["roles"]
        ]
        for channel in ("web", "mobile")
    }
