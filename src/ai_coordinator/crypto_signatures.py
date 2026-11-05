"""Cryptographic Public-Key Digital Signatures for AI Coordinator (Phase 4C.0.5).

Provides pure-Python standard library (zero-dependency) implementation of
RSASSA-PKCS1-v1_5 with SHA-256 for public authentication and independent third-party
audit of scientific baseline benchmarks and reports.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from typing import Any, Dict, Optional, Tuple, Union

# ASN.1 DigestInfo prefix for SHA-256 (RFC 8017 / PKCS #1 v2.2)
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def build_pkcs1_v15_padding(message_bytes: bytes, key_size_bytes: int = 256) -> bytes:
    """Construct deterministic EMSA-PKCS1-v1_5 encoded message for SHA-256."""
    digest = hashlib.sha256(message_bytes).digest()
    t = SHA256_DIGEST_INFO + digest
    # EMSA-PKCS1-v1_5 = 0x00 || 0x01 || PS || 0x00 || T
    # Length of PS must be key_size_bytes - len(t) - 3
    ps_len = key_size_bytes - len(t) - 3
    if ps_len < 8:
        raise ValueError(f"Key size {key_size_bytes} is too short for PKCS#1 v1.5 padding with SHA-256")
    return b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t


def rsa_sign(
    message_bytes: bytes,
    private_exponent_hex: str,
    modulus_hex: str,
    key_size_bytes: int = 256,
) -> str:
    """Sign message bytes using RSASSA-PKCS1-v1_5 SHA-256, returning hex signature."""
    em = build_pkcs1_v15_padding(message_bytes, key_size_bytes=key_size_bytes)
    em_int = int.from_bytes(em, byteorder="big")
    d = int(private_exponent_hex, 16)
    n = int(modulus_hex, 16)
    if em_int >= n:
        raise ValueError("Encoded message integer is larger than RSA modulus")
    sig_int = pow(em_int, d, n)
    sig_bytes = sig_int.to_bytes(key_size_bytes, byteorder="big")
    return sig_bytes.hex()


def rsa_verify(
    message_bytes: bytes,
    signature_hex: str,
    modulus_hex: str,
    public_exponent: int = 65537,
    key_size_bytes: int = 256,
) -> bool:
    """Verify RSASSA-PKCS1-v1_5 SHA-256 signature against public key modulus and exponent."""
    try:
        sig_bytes = bytes.fromhex(signature_hex.strip())
        if len(sig_bytes) != key_size_bytes:
            return False
        sig_int = int.from_bytes(sig_bytes, byteorder="big")
        n = int(modulus_hex, 16)
        if sig_int >= n:
            return False
        recovered_int = pow(sig_int, public_exponent, n)
        recovered_em = recovered_int.to_bytes(key_size_bytes, byteorder="big")
        expected_em = build_pkcs1_v15_padding(message_bytes, key_size_bytes=key_size_bytes)
        return hmac.compare_digest(recovered_em, expected_em)
    except Exception:
        return False


def load_public_key(pubkey_source: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Load and validate public key dictionary from path or dict."""
    if isinstance(pubkey_source, str):
        if not os.path.exists(pubkey_source):
            raise FileNotFoundError(f"Chave pública não encontrada: '{pubkey_source}'")
        with open(pubkey_source, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif isinstance(pubkey_source, dict):
        data = pubkey_source
    else:
        raise TypeError(f"Fonte de chave pública inválida: {type(pubkey_source)}")

    required = ("modulus", "public_exponent", "key_id")
    for req in required:
        if req not in data:
            raise ValueError(f"Chave pública malformada: campo obrigatório '{req}' ausente")
    return data


def load_private_key(privkey_source: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Load and validate private key dictionary from path or dict."""
    if isinstance(privkey_source, str):
        if not os.path.exists(privkey_source):
            raise FileNotFoundError(f"Chave privada não encontrada: '{privkey_source}'")
        with open(privkey_source, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif isinstance(privkey_source, dict):
        data = privkey_source
    else:
        raise TypeError(f"Fonte de chave privada inválida: {type(privkey_source)}")

    required = ("modulus", "private_exponent", "key_id")
    for req in required:
        if req not in data:
            raise ValueError(f"Chave privada malformada: campo obrigatório '{req}' ausente")
    return data
