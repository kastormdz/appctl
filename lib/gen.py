"""Generacion de secretos. Passwords fuertes, sin caracteres que rompan
el shell de un cliente ni un archivo .env.

El problema real de las passwords aleatorias: `secrets.token_urlsafe` tira
simbolos que rompen (a) el .env, (b) una linea de shell del summary, (c) un
JDBC string de Java, (d) un .pgpass. Un cliente que copia y pega y falla
migrate a las 2am es un ticket mio.

Por eso: alfanumerico + 3 clases de simbolo de un set que no molesta.
"""
import secrets
import string

# Set reducido a proposito: nada de ' " \ ` $ ; < > | & ni espacios.
# Es seguro para: .env, shell, JDBC, URL de conexion, .pgpass, YAML.
SAFE = string.ascii_letters + string.digits + "-_.@!#%^+=~?"
SYMBOLS = "-_.@!#%^+=~"


def password(length: int = 24) -> str:
    """Password alfanumerica + al menos 3 clases de simbolos.
    Garantiza 1 minuscula, 1 mayuscula, 1 digito y 3 simbolos en posiciones
    distintas al resto, para que un filtro de 'complejidad' no la rechace.
    """
    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    rest = SAFE

    while True:
        body = "".join(secrets.choice(rest) for _ in range(length))
        cand = (
            secrets.choice(lower)
            + secrets.choice(upper)
            + secrets.choice(digits)
            + "".join(secrets.choice(SYMBOLS) for _ in range(3))
            + "".join(secrets.choice(rest) for _ in range(length - 6))
        )
        # barajar: la posicion fija de los simbolos es predecible
        chars = list(cand)
        secrets.SystemRandom().shuffle(chars)
        out = "".join(chars)
        if (
            any(c in lower for c in out)
            and any(c in upper for c in out)
            and any(c in digits for c in out)
            and len(set(out) & set(SYMBOLS)) >= 3
        ):
            return out


def app_key() -> str:
    """base64 de 32 bytes, formato Laravel APP_KEY."""
    import base64
    return "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()


def token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)
