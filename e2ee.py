"""
Venice.ai End-to-End Encryption (E2EE) helpers.

Protocol: ECDH key exchange on secp256k1 → HKDF-SHA256 → AES-256-GCM.
Each request generates a fresh client keypair.  The server's public key is
obtained from the /tee/attestation endpoint.

Required packages: ecdsa>=0.18.0, cryptography>=41.0.0
"""

import os
import secrets

import requests

try:
    from ecdsa import SECP256k1, SigningKey
    from ecdsa.ellipticcurve import Point

    _ECDSA_OK = True
except ImportError:
    _ECDSA_OK = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False


def _require_deps():
    if not _ECDSA_OK:
        raise ImportError(
            "E2EE requires the 'ecdsa' package.  "
            "Install it with:  pip install ecdsa>=0.18.0"
        )
    if not _CRYPTO_OK:
        raise ImportError(
            "E2EE requires the 'cryptography' package.  "
            "Install it with:  pip install cryptography>=41.0.0"
        )


# ---------------------------------------------------------------------------
# Key generation / derivation
# ---------------------------------------------------------------------------

def generate_keypair():
    """
    Generate a fresh secp256k1 keypair for one E2EE request.

    Returns
    -------
    (signing_key, client_pub_hex)
        signing_key    : ecdsa.SigningKey object (private)
        client_pub_hex : 130-char hex string, uncompressed point (04 || x || y)
    """
    _require_deps()
    sk = SigningKey.generate(curve=SECP256k1)
    vk = sk.get_verifying_key()
    pub_hex = "04" + vk.to_string().hex()
    return sk, pub_hex


def fetch_server_pubkey(base_url, model, api_key, timeout=30):
    """
    Call GET /tee/attestation to retrieve the model's TEE public key.

    Returns the 130-char hex uncompressed public key (`signing_key` field).
    """
    nonce = secrets.token_hex(32)  # 64 hex chars = 32 bytes
    url = f"{base_url}/tee/attestation"
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"model": model, "nonce": nonce}
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # Field name varies by API version: try both known names
    key_hex = (
        data.get("signing_public_key")
        or data.get("signing_key")
        or data.get("data", {}).get("signing_public_key", "")
        or data.get("data", {}).get("signing_key", "")
    )
    if not key_hex or len(key_hex) != 130:
        raise ValueError(
            f"E2EE attestation returned unexpected signing_key: {key_hex!r}\n"
            f"Full response: {data}"
        )
    return key_hex


def derive_aes_key(client_sk, server_pub_hex):
    """
    Derive a 32-byte AES key from ECDH shared secret + HKDF-SHA256.

    Parameters
    ----------
    client_sk      : ecdsa.SigningKey (from generate_keypair)
    server_pub_hex : 130-char hex string from fetch_server_pubkey
    """
    _require_deps()
    server_bytes = bytes.fromhex(server_pub_hex)
    if len(server_bytes) != 65 or server_bytes[0] != 0x04:
        raise ValueError("E2EE: server public key must be uncompressed (65 bytes, 04 prefix)")

    x = int.from_bytes(server_bytes[1:33], "big")
    y = int.from_bytes(server_bytes[33:65], "big")

    priv_int = int.from_bytes(client_sk.to_string(), "big")
    server_point = Point(SECP256k1.curve, x, y)
    shared_point = priv_int * server_point
    shared_secret = shared_point.x().to_bytes(32, "big")

    hkdf = HKDF(algorithm=SHA256(), length=32, salt=None, info=b"ecdsa_encryption")
    return hkdf.derive(shared_secret)


# ---------------------------------------------------------------------------
# Encrypt / decrypt
# ---------------------------------------------------------------------------

def encrypt_text(text, aes_key, client_pub_hex):
    """
    Encrypt *text* with AES-256-GCM.

    Wire format (hex): client_pub(65 bytes) || nonce(12 bytes) || ciphertext+tag
    Returns a hex string.
    """
    _require_deps()
    nonce = os.urandom(12)
    aesgcm = AESGCM(aes_key)
    ciphertext = aesgcm.encrypt(nonce, text.encode("utf-8"), None)
    client_pub_bytes = bytes.fromhex(client_pub_hex)
    return (client_pub_bytes + nonce + ciphertext).hex()


def decrypt_chunk(hex_chunk, client_sk):
    """
    Decrypt one E2EE SSE chunk.

    Wire format (hex): server_pub(65 bytes) || nonce(12 bytes) || ciphertext+tag

    The server uses a fresh ephemeral keypair per chunk, so we re-derive the AES
    key for each chunk via ECDH(client_sk, chunk_server_pub) + HKDF.

    Returns the plaintext string, or None if decryption fails.
    """
    _require_deps()
    if not isinstance(hex_chunk, str) or len(hex_chunk) < 186:
        return None
    try:
        raw = bytes.fromhex(hex_chunk)
    except ValueError:
        return None
    chunk_server_pub_hex = raw[:65].hex()
    nonce = raw[65:77]
    ciphertext = raw[77:]
    try:
        chunk_aes_key = derive_aes_key(client_sk, chunk_server_pub_hex)
    except Exception:
        return None
    aesgcm = AESGCM(chunk_aes_key)
    try:
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception:
        return None


def is_encrypted_chunk(value):
    """Return True if *value* looks like an E2EE hex payload (≥186 hex chars)."""
    return (
        isinstance(value, str)
        and len(value) >= 186
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


# ---------------------------------------------------------------------------
# Message encryption helper
# ---------------------------------------------------------------------------

def encrypt_messages(messages, aes_key, client_pub_hex):
    """
    Return a new messages list with every text content encrypted.

    Handles both plain-string content and the list-of-parts format used for
    vision requests.  Image parts are left as-is.
    """
    result = []
    for msg in messages:
        new_msg = dict(msg)
        content = msg.get("content")
        if isinstance(content, str):
            new_msg["content"] = encrypt_text(content, aes_key, client_pub_hex)
        elif isinstance(content, list):
            new_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    new_part = dict(part)
                    new_part["text"] = encrypt_text(part.get("text", ""), aes_key, client_pub_hex)
                    new_parts.append(new_part)
                else:
                    new_parts.append(part)
            new_msg["content"] = new_parts
        result.append(new_msg)
    return result


# ---------------------------------------------------------------------------
# High-level request helper
# ---------------------------------------------------------------------------

def setup_e2ee_request(base_url, model, api_key, messages, timeout=30):
    """
    Perform attestation, derive keys, encrypt messages, and return the
    extra headers and the encrypted messages list.

    Parameters
    ----------
    base_url : str
        e.g. "https://api.venice.ai/api/v1"
    model    : str
        Model ID (used for attestation)
    api_key  : str
    messages : list[dict]
        Standard OpenAI-style messages list
    timeout  : int
        Timeout for the attestation request

    Returns
    -------
    (encrypted_messages, extra_headers, client_sk)
        extra_headers must be merged into the request headers.
        client_sk is needed to decrypt response chunks (each chunk carries its own
        ephemeral server pub key, so the AES key is re-derived per chunk).
    """
    _require_deps()
    client_sk, client_pub_hex = generate_keypair()
    server_pub_hex = fetch_server_pubkey(base_url, model, api_key, timeout=timeout)
    req_aes_key = derive_aes_key(client_sk, server_pub_hex)
    encrypted_messages = encrypt_messages(messages, req_aes_key, client_pub_hex)
    extra_headers = {
        "X-Venice-TEE-Client-Pub-Key": client_pub_hex,
        "X-Venice-TEE-Model-Pub-Key": server_pub_hex,
        "X-Venice-TEE-Signing-Algo": "ecdsa",
    }
    return encrypted_messages, extra_headers, client_sk
