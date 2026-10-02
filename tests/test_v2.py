import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import patch

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from starlette.testclient import TestClient

from bridge_auth import AuthConfig, CertificateVerifier, TokenVerifier, RESOURCE, AuthFailure
from bridge_v2 import create_app
from local_append import LocalServer, LocalHandler
from message_store import MAX_BODY, append_message
import server as rest


class V2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Temporary test intermediate')])
        now = datetime.now(timezone.utc)
        cls.ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(cls.ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(cls.ca_key, hashes.SHA256()))
        cls.config = AuthConfig('https://issuer.example/', 'https://issuer.example/jwks',
                                'owner-id', 'chatgpt-client-id')

    def leaf(self, san='mtls.prod.connectors.openai.com', eku=ExtendedKeyUsageOID.CLIENT_AUTH):
        now = datetime.now(timezone.utc)
        return (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test client')]))
            .issuer_name(self.ca.subject).public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(hours=1)).not_valid_after(now+timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
            .sign(self.ca_key, hashes.SHA256()))

    def token(self, **changes):
        claims = {'iss': self.config.issuer, 'sub': 'owner-id', 'aud': RESOURCE,
                  'iat': int(time.time())-1, 'exp': int(time.time())+300,
                  'azp': 'chatgpt-client-id', 'scope': 'messages:write'}
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': 'test'})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.messages = self.base/'messages.jsonl'
        self.old = {'id': 'old-note', 'created_at': '2026-01-01T00:00:00Z',
                    'agent': 'Older agent', 'message': 'Earlier public note', 'ip_hash': 'private'}
        self.messages.write_text(json.dumps(self.old)+'\n')
        self.original = self.messages.read_bytes()
        self.socket = self.base/'append.sock'
        self.local = LocalServer(str(self.socket), LocalHandler)
        self.local.messages_path = self.messages
        self.socket.chmod(0o660)
        self.thread = threading.Thread(target=self.local.serve_forever, daemon=True)
        self.thread.start()
        ca_path = self.base/'ca.pem'
        ca_path.write_bytes(self.ca.public_bytes(serialization.Encoding.PEM))
        self.certs = CertificateVerifier(ca_path)
        class Keys:
            def get_signing_key_from_jwt(inner, token):
                return SimpleNamespace(key=self.key.public_key())
        self.tokens = TokenVerifier(self.config, Keys())
        self.app = create_app(self.config, self.tokens, self.certs, str(self.socket))
        self.client = TestClient(self.app, base_url='https://agent.vincentmossman.com')
        self.client.__enter__()
        self.cert_headers = {'X-Agent-TLS-Verify': 'SUCCESS',
            'X-Agent-Client-Cert': quote(self.leaf().public_bytes(serialization.Encoding.PEM).decode()),
            'Accept': 'application/json, text/event-stream'}
        self.headers = {**self.cert_headers, 'Authorization': 'Bearer '+self.token()}

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.local.shutdown()
        self.local.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def rpc(self, method='tools/call', params=None, headers=None, path='/mcp-v2'):
        return self.client.post(path, json={'jsonrpc':'2.0','id':1,'method':method,
            'params': params if params is not None else {'name':'post_agent_message',
              'arguments': {'agent':'Test', 'message':'A temporary test note'}}},
            headers=self.headers if headers is None else headers)

    def test_discovery_and_tool_oauth_metadata(self):
        response = self.client.get('/.well-known/oauth-protected-resource/mcp-v2')
        self.assertEqual(response.json()['resource'], RESOURCE)
        self.assertEqual(response.json()['authorization_servers'], [self.config.issuer])
        self.assertEqual(response.json()['scopes_supported'], ['messages:write'])
        response = self.client.get('/.well-known/oauth-authorization-server', follow_redirects=False)
        self.assertEqual(response.status_code,307)
        self.assertEqual(response.headers['location'], self.config.issuer+'.well-known/oauth-authorization-server')
        response = self.rpc('tools/list', {}, self.cert_headers)
        self.assertEqual(response.status_code,200, response.text)
        tools = {x['name']: x for x in response.json()['result']['tools']}
        self.assertEqual(tools['post_agent_message']['_meta']['securitySchemes'],
                         [{'type':'oauth2','scopes':['messages:write']}])
        self.assertEqual(tools['post_agent_message']['securitySchemes'],
                         [{'type':'oauth2','scopes':['messages:write']}])
        self.assertEqual(tools['get_agent_messages']['_meta']['securitySchemes'], [{'type':'noauth'}])

    def test_bootstrap_denies_all_writes(self):
        from dataclasses import replace
        verifier=TokenVerifier(replace(self.config,allowed_client=''),self.tokens.keys)
        with self.assertRaises(AuthFailure):
            verifier.verify('Bearer '+self.token())

    def test_auth0_owner_policy(self):
        import subprocess
        code="""
const {onExecutePostLogin} = require('./deploy/v2/auth0-owner-only.js');
(async () => {
  for (const [owner, client, secrets, denied] of [
    ['owner','chatgpt',true,false], ['other','chatgpt',true,true],
    ['owner','other',true,true], ['owner','chatgpt',false,true]]) {
    let blocked=false;
    await onExecutePostLogin({resource_server:{identifier:'https://agent.vincentmossman.com/mcp-v2'},
      user:{user_id:owner}, client:{client_id:client},
      secrets:secrets?{AGENT_OWNER_SUBJECT:'owner',AGENT_CHATGPT_CLIENT_ID:'chatgpt'}:{}},
      {access:{deny:() => {blocked=true;}}});
    if (blocked !== denied) throw new Error('Owner policy failed');
  }
})().catch(() => process.exit(1));
"""
        subprocess.run(['node','-e',code],check=True)

    def test_anonymous_and_no_scope_challenges(self):
        response = self.rpc(headers=self.cert_headers)
        self.assertEqual(response.status_code,401)
        self.assertIn('resource_metadata=',response.headers['www-authenticate'])
        self.assertIn('mcp/www_authenticate', response.json()['result']['_meta'])
        response = self.rpc(headers={**self.cert_headers,'Authorization':'Bearer '+self.token(scope='read')})
        self.assertEqual(response.status_code,403)
        self.assertIn('insufficient_scope', response.headers['www-authenticate'])
        self.assertEqual(self.messages.read_bytes(),self.original)

    def test_invalid_tokens_do_not_append(self):
        for changes in ({'sub':'stranger'}, {'azp':'other-app'}, {'exp':int(time.time())-60},
                        {'aud':'https://other.example/'}, {'iss':'https://other-issuer.example/'},
                        {'nbf':int(time.time())+600}, {'scope':''}, {'client_id':'conflicting-client'}):
            with self.subTest(changes=changes):
                response = self.rpc(headers={**self.cert_headers, 'Authorization':'Bearer '+self.token(**changes)})
                self.assertIn(response.status_code,(401,403))
        claims = jwt.decode(self.token(), options={'verify_signature':False})
        bad_key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        forged = jwt.encode(claims,bad_key,algorithm='RS256')
        self.assertEqual(self.rpc(headers={**self.cert_headers,'Authorization':'Bearer '+forged}).status_code,401)
        del claims['exp']
        self.assertEqual(self.rpc(headers={**self.cert_headers,'Authorization':'Bearer '+jwt.encode(claims,self.key,algorithm='RS256')}).status_code,401)
        self.assertEqual(self.messages.read_bytes(),self.original)

    def test_client_certificate_required_even_with_valid_token(self):
        self.assertEqual(self.rpc(headers={'Authorization':'Bearer '+self.token()}).status_code,403)
        for leaf in (self.leaf(san='attacker.example'), self.leaf(eku=ExtendedKeyUsageOID.SERVER_AUTH)):
            headers={**self.headers,'X-Agent-Client-Cert':quote(leaf.public_bytes(serialization.Encoding.PEM).decode())}
            self.assertEqual(self.rpc(headers=headers).status_code,403)
        self.assertEqual(self.rpc(headers={**self.headers,'X-Agent-TLS-Verify':'NONE'}).status_code,403)
        self.assertEqual(self.rpc(headers={**self.headers,'X-Agent-Client-Cert':'fake'}).status_code,403)
        self.assertEqual(self.messages.read_bytes(), self.original)

    def test_valid_long_repeated_posts_and_legacy_alias(self):
        for i in range(12):
            response = self.rpc(params={'name':'post_agent_message', 'arguments': {'message':'Long '+str(i)+' '+('x'*2000)}},
                                path='/mcp' if i == 0 else '/mcp-v2')
            self.assertEqual(response.status_code,200,response.text)
            result=response.json()['result']
            self.assertFalse(result.get('isError',False),result)
            self.assertEqual(result['structuredContent']['http_status'],201)
            self.assertTrue(result['structuredContent']['receipt'])
        self.assertTrue(self.messages.read_bytes().startswith(self.original))
        self.assertEqual(len(self.messages.read_text().splitlines()),13)

    def test_request_size_empty_invalid_and_batches(self):
        for message in ('', '   ', '\x00', 123):
            response=self.rpc(params={'name':'post_agent_message','arguments':{'message':message}})
            self.assertTrue(response.json()['result'].get('isError'),response.text)
        response=self.rpc(params={'name':'post_agent_message','arguments':{'message':'x'*MAX_BODY}})
        self.assertEqual(response.status_code,413)
        response=self.client.post('/mcp-v2', content='[{}]', headers=self.headers)
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.messages.read_bytes(),self.original)

    def test_local_append_and_public_routes(self):
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(self.socket))) as client:
            for i in range(12):
                response=client.post('http://localhost/append',json={'agent':'Antigravity','message':'x'*1500})
                self.assertEqual(response.status_code,201,response.text)
            self.assertEqual(client.post('http://localhost/append', json={'message':'x'*MAX_BODY}).status_code,413)
            self.assertEqual(client.post('http://localhost/append', json={'message':' '}).status_code,400)
            self.assertEqual(client.post('http://localhost/append', content='{', headers={'Content-Type':'application/json'}).status_code,400)
        for path in ('/append','/local/append','/api/messages'):
            self.assertEqual(self.client.post(path,json={'message':'bad'},headers=self.headers).status_code,404)
        self.assertEqual(self.socket.stat().st_mode & 0o777,0o660)
        self.assertTrue(self.messages.read_bytes().startswith(self.original))

    def test_real_nginx_tls_and_untrusted_header_overwrite(self):
        import socket
        import ssl
        import subprocess
        import uvicorn
        ca_file=self.base/'ca.pem'
        key_file=self.base/'client.key'
        key_file.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_file.chmod(0o600)
        now=datetime.now(timezone.utc)
        server_cert=(x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]))
            .issuer_name(self.ca.subject).public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(hours=1)).not_valid_after(now+timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                x509.IPAddress(__import__('ipaddress').ip_address('127.0.0.1'))]),critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
            .sign(self.ca_key,hashes.SHA256()))
        cert_file=self.base/'server.pem'
        cert_file.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
        uds=self.base/'mcp.sock'
        app=create_app(self.config,self.tokens,self.certs,str(self.socket))
        asgi=uvicorn.Server(uvicorn.Config(app,uds=str(uds),log_level='critical',proxy_headers=False))
        worker=threading.Thread(target=asgi.run,daemon=True); worker.start()
        for _ in range(100):
            if asgi.started: break
            time.sleep(.05)
        self.assertTrue(asgi.started)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        proxy=(Path('deploy/v2/agent-mcp-proxy.conf').read_text()
               .replace('/run/agent-context-mcp-v2/mcp.sock',str(uds)))
        (self.base/'proxy.conf').write_text(proxy)
        # The same real nginx location template used in deployment is included.
        conf=f"""pid {self.base}/nginx.pid;
error_log {self.base}/nginx-error.log;
worker_processes 1;
events {{ worker_connections 64; }}
http {{
 access_log off;
 client_body_temp_path {self.base}/body;
 proxy_temp_path {self.base}/proxy;
 server {{
  listen 127.0.0.1:{port} ssl;
  ssl_certificate {cert_file};
  ssl_certificate_key {key_file};
  ssl_client_certificate {ca_file};
  ssl_verify_client optional;
  ssl_verify_depth 2;
  location = /mcp-v2 {{ include {self.base}/proxy.conf; }}
  location = /mcp {{ include {self.base}/proxy.conf; }}
  location = /api/messages {{ return 403; }}
  location = /append {{ return 404; }}
  location /local/ {{ return 404; }}
 }}
}}
"""
        config_path=self.base/'nginx.conf'; config_path.write_text(conf)
        proc=subprocess.Popen(['/usr/sbin/nginx','-p',str(self.base),'-c',str(config_path),'-g','daemon off;'],
                              stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            ssl_context=ssl.create_default_context(cafile=str(ca_file))
            url=f'https://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1): break
                except OSError: time.sleep(.05)
            self.assertIsNone(proc.poll(), (self.base/'nginx-error.log').read_text())
            payload={'jsonrpc':'2.0','id':1,'method':'tools/call',
                     'params':{'name':'post_agent_message','arguments':{'message':'TLS integration note in temporary storage'}}}
            with httpx.Client(verify=ssl_context,trust_env=False) as client:
                # A caller cannot spoof the supposedly trusted TLS headers.
                self.assertEqual(client.post(url+'/mcp-v2',json=payload,headers=self.headers).status_code,403)
                self.assertEqual(client.post(url+'/api/messages',json={'message':'bad'}).status_code,403)
                self.assertEqual(client.post(url+'/append',json={'message':'bad'}).status_code,404)
                self.assertEqual(client.post(url+'/local/append',json={'message':'bad'}).status_code,404)
            bad_ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            untrusted=(x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Untrusted')]))
                .issuer_name(self.ca.subject).public_key(self.key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now-timedelta(hours=1)).not_valid_after(now+timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('mtls.prod.connectors.openai.com')]),critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),critical=False)
                .sign(bad_ca_key,hashes.SHA256()))
            bad_leaf=self.base/'untrusted.pem'
            bad_leaf.write_bytes(untrusted.public_bytes(serialization.Encoding.PEM))
            ssl_context=ssl.create_default_context(cafile=str(ca_file))
            ssl_context.load_cert_chain(str(bad_leaf),str(key_file))
            with httpx.Client(verify=ssl_context,trust_env=False) as client:
                response=client.post(url+'/mcp-v2',json=payload,headers=self.headers)
                self.assertEqual(response.status_code,400,response.text)
            for san, expected in (('wrong.example',403),('mtls.prod.connectors.openai.com',200)):
                leaf=self.base/'leaf.pem'
                leaf.write_bytes(self.leaf(san=san).public_bytes(serialization.Encoding.PEM))
                ssl_context=ssl.create_default_context(cafile=str(ca_file))
                ssl_context.load_cert_chain(str(leaf),str(key_file))
                with httpx.Client(verify=ssl_context,trust_env=False) as client:
                    response=client.post(url+'/mcp-v2',json=payload,headers=self.headers)
                    self.assertEqual(response.status_code,expected,response.text)
                    if expected==200:
                        self.assertEqual(response.json()['result']['structuredContent']['http_status'],201)
            self.assertEqual(len(self.messages.read_text().splitlines()),2)
        finally:
            proc.terminate(); proc.communicate(timeout=5)
            asgi.should_exit=True; worker.join(timeout=5)

    def test_mcp_reads_preserve_public_results_without_oauth(self):
        registry=self.base/'read-redactions.json'; registry.write_text('{}')
        with patch.object(rest,'MESSAGES_PATH',self.messages), patch.object(rest,'REDACTIONS_PATH',registry):
            http=rest.ThreadingHTTPServer(('127.0.0.1',0),rest.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True); thread.start()
            try:
                app=create_app(self.config,self.tokens,self.certs,str(self.socket),
                               rest_base=f'http://127.0.0.1:{http.server_port}')
                with TestClient(app,base_url='https://agent.vincentmossman.com') as client:
                    for name in ('get_agent_context','get_agent_messages'):
                        response=client.post('/mcp-v2',headers=self.cert_headers,
                            json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                                  'params':{'name':name,'arguments':{}}})
                        self.assertEqual(response.status_code,200,response.text)
                        result=response.json()['result']
                        self.assertFalse(result.get('isError',False),result)
                        if name=='get_agent_messages':
                            self.assertEqual(result['structuredContent']['messages'][0]['message'],self.old['message'])
                        else:
                            self.assertIn('projects',result['structuredContent'])
                self.assertEqual(self.messages.read_bytes(),self.original)
            finally:
                http.shutdown(); http.server_close(); thread.join()

    def test_rest_cutover_read_preservation_and_redaction(self):
        registry=self.base/'redactions.json'
        registry.write_text(json.dumps({'old-note':'Owner-redacted public note'}))
        with patch.object(rest,'MESSAGES_PATH',self.messages), patch.object(rest,'REDACTIONS_PATH',registry), patch.dict(os.environ, {'AGENT_PUBLIC_WRITES':'0'}):
            http=rest.ThreadingHTTPServer(('127.0.0.1',0),rest.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True)
            thread.start()
            try:
                origin=f'http://127.0.0.1:{http.server_port}'
                with httpx.Client(trust_env=False) as client:
                    self.assertEqual(client.post(origin+'/api/messages',json={'message':'bad'}).status_code,403)
                    for path in ('/append','/local/append'):
                        self.assertEqual(client.post(origin+path,json={'message':'bad'}).status_code,404)
                    response=client.get(origin+'/messages.json')
                    self.assertEqual(response.status_code,200)
                    note=response.json()['messages'][0]
                    self.assertEqual(note['message'],'Owner-redacted public note')
                    self.assertTrue(note['redacted'])
                    self.assertNotIn('ip_hash',note)
                    self.assertEqual(client.get(origin+'/context.json').status_code,200)
                self.assertEqual(self.messages.read_bytes(),self.original)
            finally:
                http.shutdown(); http.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
