"""Cloud node certificate matching must happen before application data is sent."""
import asyncio
import datetime
import ssl
import tempfile
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from module.device.cloud.core.session import _CloudTLSObject


class CloudTLSTests(unittest.IsolatedAsyncioTestCase):
    async def test_underscore_wildcard_is_accepted_but_other_host_is_rejected_before_send(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '*.example.test')])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - datetime.timedelta(minutes=1))
                    .not_valid_after(now + datetime.timedelta(days=1))
                    .add_extension(x509.SubjectAlternativeName([x509.DNSName('*.example.test')]), critical=False)
                    .sign(key, hashes.SHA256()))
            cert_path, key_path = root / 'cert.pem', root / 'key.pem'
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                                  serialization.NoEncryption()))
            server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_tls.load_cert_chain(cert_path, key_path)
            client_tls = ssl.create_default_context(cafile=str(cert_path))
            client_tls.check_hostname = False
            client_tls.sslobject_class = _CloudTLSObject
            received = asyncio.Queue()

            async def accept(reader, writer):
                try:
                    await received.put(await reader.read(64))
                finally:
                    writer.close()
                    await writer.wait_closed()

            async with await asyncio.start_server(accept, '127.0.0.1', 0, ssl=server_tls) as server:
                port = server.sockets[0].getsockname()[1]
                _reader, writer = await asyncio.open_connection('127.0.0.1', port,
                                                               ssl=client_tls, server_hostname='cloud_node.example.test')
                writer.write(b'test-application-data')
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                self.assertEqual(await asyncio.wait_for(received.get(), 2), b'test-application-data')
                with self.assertRaises(ssl.CertificateError):
                    await asyncio.open_connection('127.0.0.1', port,
                                                  ssl=client_tls, server_hostname='cloud_node.other.test')
                if not received.empty():
                    self.assertEqual(received.get_nowait(), b'')
                untrusted_tls = ssl.create_default_context()
                untrusted_tls.check_hostname = False
                untrusted_tls.sslobject_class = _CloudTLSObject
                with self.assertRaises(ssl.SSLCertVerificationError):
                    await asyncio.open_connection('127.0.0.1', port,
                                                  ssl=untrusted_tls, server_hostname='cloud_node.example.test')


if __name__ == '__main__':
    unittest.main()
