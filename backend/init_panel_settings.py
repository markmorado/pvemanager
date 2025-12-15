"""
Initialize panel_settings table with default values
Run this script once to create the table and add default settings
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from app.db import SessionLocal, engine, Base
from app.models import PanelSettings

def init_panel_settings():
    """Initialize panel settings table"""
    print("Creating panel_settings table...")
    
    # Create table
    Base.metadata.create_all(bind=engine)
    
    # Add default settings
    db = SessionLocal()
    try:
        # Check if settings already exist
        existing = db.query(PanelSettings).count()
        if existing > 0:
            print(f"Panel settings already initialized ({existing} settings found)")
            return
        
        # Add default settings
        defaults = [
            PanelSettings(
                key="refresh_interval",
                value="5",
                description="Интервал обновления данных на панели (секунды)"
            ),
            PanelSettings(
                key="log_retention_days",
                value="30",
                description="Срок хранения логов перед автоматической очисткой (дни)"
            ),
        ]
        
        for setting in defaults:
            db.add(setting)
        
        db.commit()
        print(f"✓ Added {len(defaults)} default settings")
        
        # Display settings
        print("\nDefault settings:")
        for setting in defaults:
            print(f"  - {setting.key}: {setting.value} ({setting.description})")
        
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    init_panel_settings()
    print("\n✓ Panel settings initialized successfully")
