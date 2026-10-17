import re

COUNTRY_PHONE_RULES = {
    "+52": (10, 10), "+1": (10, 10), "+57": (10, 10),
    "+54": (10, 10), "+34": (9, 9),
}

def normalize_phone(value, country_code="+52"):
    """Normalize supported public-registration numbers to E.164 without rewriting stored data."""
    raw = (value or "").strip()
    if not raw:
        return None
    compact = re.sub(r"[\s().-]", "", raw)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if compact.startswith("+"):
        matched = next((code for code in sorted(COUNTRY_PHONE_RULES, key=len, reverse=True) if compact.startswith(code)), None)
        if not matched:
            raise ValueError("El prefijo telefónico no está permitido.")
        country_code, local = matched, compact[len(matched):]
    else:
        if country_code not in COUNTRY_PHONE_RULES:
            raise ValueError("Selecciona un país válido.")
        local = compact
    if not local.isdigit():
        raise ValueError("El teléfono solo puede contener dígitos y separadores comunes.")
    minimum, maximum = COUNTRY_PHONE_RULES[country_code]
    if not minimum <= len(local) <= maximum:
        raise ValueError("El número telefónico no tiene una longitud válida para el país seleccionado.")
    return f"{country_code}{local}"
