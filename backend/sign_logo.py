#!/usr/bin/env python3
"""
Logo Signing Script for PVEmanager
==================================

This script signs the logo with your PRIVATE key.
The private key must NEVER be shared or committed to the repository!

Usage:
    python sign_logo.py <logo.png> <private_key.pem>
    
Example:
    python sign_logo.py my_logo.png ~/.pvemanager_private_key.pem
    
Output:
    Creates logo.sig file alongside the logo

Files to commit to repository:
    - logo.png (the logo image)
    - logo.sig (the signature file)
    
Files to NEVER commit:
    - *.pem (private key files)
    - Add to .gitignore: *.pem, private_key*
"""

import sys
import os
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app.services.logo_service import sign_logo_with_private_key
except ImportError:
    # Fallback if running standalone
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import serialization
    import base64
    
    def sign_logo_with_private_key(logo_path, private_key_path, output_sig_path=None):
        logo_path = Path(logo_path)
        private_key_path = Path(private_key_path)
        
        if not logo_path.exists():
            return False, f"Logo not found: {logo_path}"
        if not private_key_path.exists():
            return False, f"Private key not found: {private_key_path}"
        
        logo_data = logo_path.read_bytes()
        
        if not logo_data[:8] == b'\x89PNG\r\n\x1a\n':
            return False, "Not a valid PNG file"
        
        private_key = serialization.load_pem_private_key(
            private_key_path.read_bytes(),
            password=None
        )
        
        signature = private_key.sign(
            logo_data,
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        
        sig_path = Path(output_sig_path) if output_sig_path else logo_path.with_suffix('.sig')
        sig_b64 = base64.b64encode(signature).decode()
        sig_path.write_text(sig_b64)
        
        return True, f"Signature saved to: {sig_path}"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        print("\nError: Missing arguments")
        print("Usage: python sign_logo.py <logo.png> <private_key.pem>")
        sys.exit(1)
    
    logo_path = sys.argv[1]
    private_key_path = sys.argv[2]
    output_path = sys.argv[3] if len(sys.argv) > 3 else None
    
    print(f"Signing logo: {logo_path}")
    print(f"Using private key: {private_key_path}")
    
    success, message = sign_logo_with_private_key(logo_path, private_key_path, output_path)
    
    if success:
        print(f"✓ {message}")
        print("\nDon't forget to:")
        print("  1. Copy logo.png and logo.sig to backend/app/static/img/")
        print("  2. Commit both files to the repository")
        print("  3. NEVER commit the private key!")
    else:
        print(f"✗ Error: {message}")
        sys.exit(1)


if __name__ == '__main__':
    main()
