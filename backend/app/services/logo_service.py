"""
Logo Protection Service with RSA Digital Signature
===================================================

Protects the panel logo from unauthorized modifications using RSA digital signatures.

How it works:
1. Logo creator signs the logo with their PRIVATE key (kept secret, never shared)
2. PUBLIC key is embedded in this code
3. On each request, signature is verified with public key
4. Without the private key, it's cryptographically impossible to create a valid signature

For logo updates: Contact the project maintainer with the new logo.
"""

import base64
import os
from pathlib import Path
from loguru import logger

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import serialization
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logger.warning("cryptography library not available, logo protection disabled")

# Get the static directory path
STATIC_DIR = Path(__file__).parent.parent / "static"
LOGO_PATH = STATIC_DIR / "img" / "logo.png"
LOGO_SIG_PATH = STATIC_DIR / "img" / "logo.sig"

# ============================================================
# PUBLIC KEY - Safe to be in open source code
# This key can only VERIFY signatures, not create them
# ============================================================
PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvM0VCWkP0fg2PMAPYJdn
JF0YO9EkJtJmnhV2v+VIIqqXdiBnNDVFHxAxxDmeMHkXff9VbZ5PRRDc/AMRSjwY
f1tN/xCCfQG8GVNlIKDcr11GJCAm7vOjy64bSEs/VpOK/A0l4js0FX6o/nEvqDqF
hSdrxhrnX4iWbm6UaXL5ZcWrHYXXyD0zsc2g5xEXAtf7sy8U8bHtD6NjaLNNIVWz
LzZqNTaAzOKXKOgKdwsYT4JXvWNLgrjMHo5TUcYDAnrPDSE2E6Ms2TZDS0BicB7Q
g6FZ5M8H9NrttpDxvS0Gwen0AfRbI/0Cocn9nG88eiX/hbsodPxmJIlloAUOhtRa
dwIDAQAB
-----END PUBLIC KEY-----"""

# Fallback logo (simple base64 encoded placeholder - 64x64 transparent PNG)
FALLBACK_LOGO_B64 = """
iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAACXBIWXMAAAsTAAALEwEAmpwYAAAD
GUlEQVR4nO2bS27bMBCGP7RZdNNFb9BFT+Au0hv0Br1BeoMe4QSNF+4q3fQGXfQG3fQEi+xatQ8g
kCIlPkTJUuL5gICRPMP5OZwhKQOGYRiGYRiGYRiGYRhGIpI0dQ/nOy64QC4F/gI/gO/A17LkoyAJ
cAX4AFwCF8AvYAf8Bn4C38o/+xaQBLgGPgNXZbkA/gDb8s+2wC/gS1nykZAEvAXeA2/Ksg1sgB/l
n+2ALfBpmXIXBUnAO+AD8Los62AB/C7/bAN8L0s+CpKAG+A98LosK+Av8Kf8sw3wo/yzb4IkwDXw
CXhdljXwB/hb/tkW+FWWfBQkAa6BT8DrsmyAX8Dv8s82wM+y5CMhCXANfAJelWUN/AZ+lX+2Bn6U
JR8JSYBr4CPwqixr4Bfwu/yzNfCjLPlISAJcAx+BV2VZAb+A3+WfrYEfZclHQhLgGvgAvCrLEvgJ
/Cr/bAn8KEs+EpIA18B74GVZFsAP4Ff5ZwvgR1nykZAEuAbeAy/LMgd+AD/LP5sDX8uSj4QkwDXw
DnhZljnwA/hZ/tkc+FKWfCQkAa6Bd8DLskyBb8DP8s+mwJey5CMhCXANvAVelmUCfAN+ln82Ab6U
JR8JSYBr4A3wsiw3wFfgR/lnN8CXsuQjIQlwDbwBXpTlGvgK/Cj/7Br4UpZ8JCQBroHXwIuyXAFf
gB/ln10Bn8uSj4QkwBXwGnhRlkvgM/Cj/LNL4HNZ8pGQBLgCXgMvyjIGPgM/yj8bA5/Lko+EJMAV
8Bp4UZYx8An4Uf7ZGPhclnwkJAEugdfAi7JcAJ+AH+WfXQCfy5KPhCTAJfAaeFGWMfAR+FH+2Rj4
VJZ8JCQBLoHXwIuyjIEPwI/yz8bAx7LkIyEJcAm8Bp6XZQS8B76Xfz4C3pclHwlJgAvgNfC8LEPg
HfC9/PMh8K4s+UhIAlwAr4HnZRkCb4Hv5Z8PgbdlyUdCEuACeA08L8sQeAN8L/98CLwpSz4SkgAX
wCvgeVkGwGvge/nnA+B1WfKRkAS4AF4Bz8syAF4B38s/HwCvypKPhGEYhmEYhmEYhmEYhmH8D/wD
F0bN8E0rJrsAAAAASUVORK5CYII=
""".strip().replace('\n', '')


class LogoService:
    """Service for logo protection using RSA digital signatures"""
    
    _public_key = None
    
    @classmethod
    def _get_public_key(cls):
        """Load and cache public key"""
        if cls._public_key is None and CRYPTO_AVAILABLE:
            try:
                cls._public_key = serialization.load_pem_public_key(
                    PUBLIC_KEY_PEM.encode()
                )
            except Exception as e:
                logger.error(f"Failed to load public key: {e}")
        return cls._public_key
    
    @staticmethod
    def verify_signature(data: bytes, signature: bytes) -> bool:
        """Verify RSA signature of data"""
        if not CRYPTO_AVAILABLE:
            return True  # Skip verification if crypto not available
        
        public_key = LogoService._get_public_key()
        if not public_key:
            return False
        
        try:
            public_key.verify(
                signature,
                data,
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            return True
        except Exception as e:
            logger.warning(f"Signature verification failed: {e}")
            return False
    
    @staticmethod
    def get_stored_signature() -> bytes | None:
        """Get stored signature from file"""
        try:
            if LOGO_SIG_PATH.exists():
                # Signature is stored as base64
                sig_b64 = LOGO_SIG_PATH.read_text().strip()
                return base64.b64decode(sig_b64)
        except Exception as e:
            logger.error(f"Error reading logo signature: {e}")
        return None
    
    @staticmethod
    def verify_logo() -> bool:
        """Verify logo integrity using RSA signature"""
        try:
            if not LOGO_PATH.exists():
                logger.warning("Logo file not found")
                return False
            
            signature = LogoService.get_stored_signature()
            if not signature:
                logger.warning("No signature found for logo")
                return False
            
            logo_data = LOGO_PATH.read_bytes()
            
            if not LogoService.verify_signature(logo_data, signature):
                logger.warning("Logo signature verification FAILED - unauthorized modification detected!")
                return False
            
            logger.debug("Logo signature verified successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error verifying logo: {e}")
            return False
    
    @staticmethod
    def get_logo_bytes() -> bytes:
        """Get logo bytes - verified original or fallback"""
        if LogoService.verify_logo():
            return LOGO_PATH.read_bytes()
        else:
            logger.info("Using fallback logo (signature verification failed)")
            return base64.b64decode(FALLBACK_LOGO_B64)
    
    @staticmethod
    def get_logo_base64() -> str:
        """Get logo as base64 string"""
        return base64.b64encode(LogoService.get_logo_bytes()).decode()
    
    @staticmethod
    def is_logo_valid() -> bool:
        """Check if current logo is valid (has valid signature)"""
        return LogoService.verify_logo()


# ============================================================
# SIGNING FUNCTIONS - Only work with private key
# These are used by the sign_logo.py script
# ============================================================

def sign_logo_with_private_key(logo_path: str, private_key_path: str, output_sig_path: str = None) -> tuple[bool, str]:
    """
    Sign a logo file with private key.
    This function is for the logo creator only.
    
    Args:
        logo_path: Path to the logo PNG file
        private_key_path: Path to the private key PEM file
        output_sig_path: Path for output signature file (default: logo_path + '.sig')
    
    Returns:
        tuple (success, message)
    """
    if not CRYPTO_AVAILABLE:
        return False, "cryptography library not installed"
    
    try:
        # Load logo
        logo_path = Path(logo_path)
        if not logo_path.exists():
            return False, f"Logo file not found: {logo_path}"
        
        logo_data = logo_path.read_bytes()
        
        # Verify it's a PNG
        if not logo_data[:8] == b'\x89PNG\r\n\x1a\n':
            return False, "File is not a valid PNG"
        
        # Load private key
        private_key_path = Path(private_key_path)
        if not private_key_path.exists():
            return False, f"Private key not found: {private_key_path}"
        
        private_key = serialization.load_pem_private_key(
            private_key_path.read_bytes(),
            password=None
        )
        
        # Create signature
        signature = private_key.sign(
            logo_data,
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        
        # Save signature as base64
        sig_path = Path(output_sig_path) if output_sig_path else logo_path.with_suffix('.sig')
        sig_b64 = base64.b64encode(signature).decode()
        sig_path.write_text(sig_b64)
        
        return True, f"Signature saved to: {sig_path}"
        
    except Exception as e:
        return False, str(e)
