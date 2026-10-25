from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class QQSignature:
    def __init__(self, secret: str):
        if not secret:
            raise ValueError("empty QQ secret")
        seed = secret.encode()
        while len(seed) < 32:
            seed *= 2
        self.key = Ed25519PrivateKey.from_private_bytes(seed[:32])

    def verify(self, timestamp: str, body: bytes, signature: str) -> bool:
        if not timestamp or not signature:
            return False
        try:
            self.key.public_key().verify(bytes.fromhex(signature), timestamp.encode() + body)
            return True
        except (ValueError, InvalidSignature):
            return False

    def challenge(self, plain_token: str, event_ts: str) -> dict:
        return {"plain_token": plain_token,
                "signature": self.key.sign((event_ts + plain_token).encode()).hex()}
