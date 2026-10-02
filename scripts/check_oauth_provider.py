#!/usr/bin/env python3
"""Read-only deployment preflight; never requests or prints tokens."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from bridge_auth import AuthConfig, https_url


def check(config):
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
        response = client.get(config.issuer.rstrip('/')+'/.well-known/oauth-authorization-server')
        response.raise_for_status()
        metadata = response.json()
        if metadata.get('issuer') != config.issuer:
            raise ValueError('Issuer does not match provider discovery exactly')
        if 'S256' not in metadata.get('code_challenge_methods_supported', []):
            raise ValueError('Provider must advertise PKCE S256')
        if 'code' not in metadata.get('response_types_supported', []):
            raise ValueError('Provider must support the authorization-code flow')
        if config.registration_method == 'cimd' and metadata.get('client_id_metadata_document_supported') is not True:
            raise ValueError('CIMD is not enabled in provider discovery')
        for key in ('authorization_endpoint','token_endpoint','jwks_uri'):
            https_url(metadata[key])
        if metadata['jwks_uri'] != config.jwks_uri:
            raise ValueError('Configured JWKS must match provider discovery')
        response = client.get(config.jwks_uri)
        response.raise_for_status()
        if not any(k.get('kty') == 'RSA' and k.get('alg', 'RS256') == 'RS256' for k in response.json().get('keys', [])):
            raise ValueError('Provider must publish an RS256 signing key')
    print('Provider discovery and JWKS preflight passed. Owner login, S256 enforcement, resource audience and ChatGPT linking still require an end-to-end authorization test.')
    if not config.allowed_client:
        print('Discovery-only bootstrap: all writes are denied until the exact ChatGPT client is configured.')


if __name__ == '__main__':
    check(AuthConfig.load(sys.argv[1]))
