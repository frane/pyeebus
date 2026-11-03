"""SHIP certificates and SKIs (SHIP 12.1).

Every SHIP node has a self-signed ECDSA P-256 certificate. Its Subject Key
Identifier (SKI) is the SHA-1 of the uncompressed public key point and is the
node's identity: trust between nodes is established per SKI.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# SHIP 9.1: the first is mandatory, the second optional. OpenSSL names.
CIPHERS = "ECDHE-ECDSA-AES128-SHA256:ECDHE-ECDSA-AES128-GCM-SHA256"


class InvalidSkiError(ValueError):
    """Certificate SKI is missing or does not match its public key."""


def normalize_ski(ski: str) -> str:
    return ski.replace(" ", "").replace("-", "").replace(":", "").lower()


def is_ski_valid(ski: str) -> bool:
    ski = normalize_ski(ski)
    return len(ski) == 40 and all(c in "0123456789abcdef" for c in ski)


def _ski_from_key(key: ec.EllipticCurvePublicKey) -> bytes:
    point = key.public_bytes(serialization.Encoding.X962,
                             serialization.PublicFormat.UncompressedPoint)
    return hashlib.sha1(point).digest()  # noqa: S324 - mandated by SHIP


def _tlv(data: bytes, pos: int) -> tuple[int, int, int]:
    """One DER/BER element at ``pos``: (tag, start of value, end of value)."""
    tag, length = data[pos], data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        if not 0 < count <= 4:
            raise ValueError("unsupported ASN.1 length")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if pos + length > len(data):
        raise ValueError("truncated ASN.1 element")
    return tag, pos, pos + length


def _children(data: bytes, start: int, end: int):
    while start < end:
        tag, value, start = _tlv(data, start)
        yield tag, value, start


_SKI_OID = bytes.fromhex("551d0e")  # 2.5.29.14


def _ski_extension(der: bytes) -> bytes | None:
    """The SubjectKeyIdentifier, read without strict DER rules.

    Some wallboxes (Elli Gen 1, e.g. Skoda iV / VW ID. Charger, firmware 3.9) encode
    BasicConstraints ``cA`` TRUE as 0x01 instead of 0xFF. OpenSSL, Go and Rust accept
    that, ``cryptography`` refuses to parse any extension of such a certificate.
    """
    _, cert_start, cert_end = _tlv(der, 0)
    _, tbs_start, tbs_end = next(_children(der, cert_start, cert_end))
    for tag, start, end in _children(der, tbs_start, tbs_end):
        if tag != 0xA3:  # [3] extensions
            continue
        _, list_start, list_end = _tlv(der, start)
        for _, ext_start, ext_end in _children(der, list_start, list_end):
            parts = list(_children(der, ext_start, ext_end))
            if parts[0][0] != 0x06 or der[parts[0][1]:parts[0][2]] != _SKI_OID:
                continue
            _, value_start, _ = parts[-1]  # extnValue OCTET STRING
            tag, ski_start, ski_end = _tlv(der, value_start)
            return der[ski_start:ski_end] if tag == 0x04 else None
    return None


def ski_from_certificate(der: bytes) -> str:
    """Return the SKI (40 hex chars) of a peer certificate, validating it."""
    cert = x509.load_der_x509_certificate(der)
    key = cert.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise InvalidSkiError("SHIP requires an ECDSA certificate")
    try:
        ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
    except x509.ExtensionNotFound:
        ski = None
    except ValueError:  # an extension that is not strict DER, see _ski_extension
        try:
            ski = _ski_extension(der)
        except (ValueError, IndexError, StopIteration) as err:
            raise InvalidSkiError(f"cannot read the certificate's SKI: {err}") from err
    if ski is None:
        raise InvalidSkiError("certificate has no SKI")
    if len(ski) != 20 or ski != _ski_from_key(key):
        raise InvalidSkiError("certificate SKI does not match its public key")
    return ski.hex()


def fingerprint_from_certificate(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


@dataclass(frozen=True)
class Identity:
    """Local certificate and key (PEM) plus the derived SKI."""

    cert_pem: bytes
    key_pem: bytes

    @property
    def cert_der(self) -> bytes:
        return x509.load_pem_x509_certificate(self.cert_pem).public_bytes(
            serialization.Encoding.DER)

    @property
    def ski(self) -> str:
        return ski_from_certificate(self.cert_der)

    @classmethod
    def create(cls, common_name: str, organization: str = "pyeebus",
               organizational_unit: str = "pyeebus", country: str = "DE") -> Identity:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organizational_unit),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.COUNTRY_NAME, country),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(secrets.randbits(130))
            .not_valid_before(now)
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier(_ski_from_key(key.public_key())),
                           critical=False)
            .sign(key, hashes.SHA256())
        )
        return cls(
            cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()),
        )

    @classmethod
    def load_or_create(cls, directory: str | Path, common_name: str) -> Identity:
        """Keep the identity in ``directory`` (cert.pem, key.pem) across restarts."""
        directory = Path(directory)
        cert_file, key_file = directory / "cert.pem", directory / "key.pem"
        if cert_file.exists() and key_file.exists():
            return cls(cert_file.read_bytes(), key_file.read_bytes())
        identity = cls.create(common_name)
        directory.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(identity.key_pem)
        key_file.chmod(0o600)
        cert_file.write_bytes(identity.cert_pem)
        return identity
