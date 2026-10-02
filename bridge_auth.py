"""Resource-server validation only. OAuth grants are handled by the external IdP."""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, unquote

import jwt
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

WRITE_SCOPE = 'messages:write'
PUBLIC_ORIGIN = 'https://agent.vincentmossman.com'
RESOURCE = PUBLIC_ORIGIN + '/mcp-v2'
METADATA_URL = PUBLIC_ORIGIN + '/.well-known/oauth-protected-resource/mcp-v2'


class AuthFailure(Exception):
    def __init__(self, status=401, error='invalid_token'):
        self.status, self.error = status, error


def https_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError('A canonical HTTPS URL is required')
    return value


@dataclass(frozen=True)
class AuthConfig:
    issuer: str
    jwks_uri: str
    allowed_subject: str
    allowed_client: str
    registration_method: str = 'cimd'

    @classmethod
    def load(cls, path):
        value = json.loads(Path(path).read_text())
        config = cls(**value)
        https_url(config.issuer)
        https_url(config.jwks_uri)
        if urlsplit(config.issuer).netloc != urlsplit(config.jwks_uri).netloc:
            raise ValueError('JWKS must be on the configured issuer host')
        if not config.allowed_subject or 'REPLACE' in repr(config):
            raise ValueError('Set the exact owner subject; placeholder configuration is not allowed')
        # Empty client supports discovery during registration, never posting.
        if config.registration_method not in ('cimd', 'pre_registered'):
            raise ValueError('Unsupported client registration method')
        return config


def challenge(error=None):
    value = f'Bearer resource_metadata="{METADATA_URL}", scope="{WRITE_SCOPE}"'
    if error:
        value += f', error="{error}", error_description="Authorize the configured account with messages:write"'
    return value


class TokenVerifier:
    def __init__(self, config, key_client=None):
        self.config = config
        self.keys = key_client or jwt.PyJWKClient(config.jwks_uri, cache_keys=False,
                                                lifespan=300, timeout=5)

    def verify(self, authorization):
        if not authorization or not authorization.startswith('Bearer '):
            raise AuthFailure()
        token = authorization[7:]
        if not token or len(token) > 16384:
            raise AuthFailure()
        try:
            # Only the configured JWKS is used; token-supplied jku/x5u are ignored.
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=['RS256'],
                audience=RESOURCE, issuer=self.config.issuer,
                options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'scope']})
        except (jwt.PyJWTError, ValueError, OSError):
            raise AuthFailure() from None
        if not self.config.allowed_client or claims['sub'] != self.config.allowed_subject:
            raise AuthFailure(403, 'access_denied')
        client = claims.get('azp', claims.get('client_id'))
        if client != self.config.allowed_client or ('client_id' in claims and claims['client_id'] != client):
            raise AuthFailure(403, 'access_denied')
        if not isinstance(claims['scope'], str) or WRITE_SCOPE not in claims['scope'].split():
            raise AuthFailure(403, 'insufficient_scope')
        return claims


class CertificateVerifier:
    """nginx verifies the TLS chain; verify the precise leaf identity as well.

    These headers are accepted ONLY on the nginx-owned Unix-socket ingress.
    The nginx location always overwrites them with TLS-derived values.
    """
    def __init__(self, intermediate_path):
        self.intermediate = x509.load_pem_x509_certificate(Path(intermediate_path).read_bytes())

    def verify(self, headers):
        if headers.get('x-agent-tls-verify') != 'SUCCESS':
            raise AuthFailure(403, 'chatgpt_client_required')
        value = headers.get('x-agent-client-cert', '')
        if not value or len(value) > 16384:
            raise AuthFailure(403, 'chatgpt_client_required')
        try:
            cert = x509.load_pem_x509_certificate(unquote(value).encode('ascii'))
            cert.verify_directly_issued_by(self.intermediate)
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            try:
                if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                    raise ValueError('A leaf certificate is required')
            except x509.ExtensionNotFound:
                pass
            now = datetime.now(timezone.utc)
            if ('mtls.prod.connectors.openai.com' not in names.get_values_for_type(x509.DNSName)
                    or ExtendedKeyUsageOID.CLIENT_AUTH not in eku
                    or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc):
                raise ValueError('Wrong certificate identity')
        except Exception:
            raise AuthFailure(403, 'chatgpt_client_required') from None
