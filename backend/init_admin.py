#!/usr/bin/env python3
"""
Initialize admin user with full RBAC v2 permissions.
Creates admin user and assigns 'admin' role from database.
"""
import sys
import os

# Add app directory to path
sys.path.insert(0, '/app')

from app.db import get_db
from app.models import User, Role
from app.auth import get_password_hash

def create_default_admin():
    """Create admin user with default credentials and full RBAC permissions"""
    username = "admin"
    email = "admin@example.com"
    password = "admin123"
    
    db = next(get_db())
    
    try:
        # Check if admin exists
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            print(f"✅ Admin user '{username}' already exists")
            
            # Ensure admin has role assigned
            if not existing.role_id:
                admin_role = db.query(Role).filter(Role.name == 'admin').first()
                if admin_role:
                    existing.role_id = admin_role.id
                    db.commit()
                    print(f"   ✅ Assigned 'admin' role with full RBAC permissions")
            return
        
        # Get admin role from database (created in init.sql)
        admin_role = db.query(Role).filter(Role.name == 'admin').first()
        
        # Create admin
        hashed_password = get_password_hash(password)
        admin = User(
            username=username,
            email=email,
            hashed_password=hashed_password,
            is_admin=True,
            is_active=True,
            role_id=admin_role.id if admin_role else None
        )
        
        db.add(admin)
        db.commit()
        
        print("✅ Admin user created successfully!")
        print(f"   Username: {username}")
        print(f"   Email: {email}")
        print(f"   Password: {password}")
        if admin_role:
            print(f"   Role: {admin_role.display_name} (full RBAC permissions)")
        print(f"\n🔗 Login at: http://localhost:8000/login")
        
        # Show RBAC permissions summary
        if admin_role and admin_role.permissions:
            perm_count = len([k for k, v in admin_role.permissions.items() if v])
            print(f"\n📋 RBAC Permissions: {perm_count} permissions granted")
            print("   Including: server:*, vm:*, lxc:*, template:*, user:*, role:*, setting:*, log:*, etc.")
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error creating admin: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    create_default_admin()
